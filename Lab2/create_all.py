#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab Practice 3 - Managed Instance Groups y Autoscaling (Google Cloud)

Despliega TODO lo que pide la práctica usando gcloud (pensado para Cloud Shell).
NO borra nada: para limpiar, ejecuta después lab3_cleanup.py.

Es idempotente: si un recurso ya existe, lo salta; puedes relanzarlo sin miedo.

NOTA: Google descontinuó "instance-templates create-with-container". Las plantillas
se crean ahora con Container-Optimized OS (cos-stable) y un startup script que
lanza el contenedor con Docker.

Uso (el proyecto se CREA SOLO; --project-id es opcional):
  python3 lab3_deploy.py
  python3 lab3_deploy.py --project-id lab-3-a-48213
  python3 lab3_deploy.py --project-id lab-3-a-48213 --phase task1
  python3 lab3_deploy.py --project-id lab-3-a-48213 --phase step9
  python3 lab3_deploy.py --project-id lab-3-a-48213 --phase task2
  python3 lab3_deploy.py --project-id lab-3-a-48213 --phase task3 --image ~/mifoto.jpg
  python3 lab3_deploy.py --project-id lab-3-a-48213 --phase task4

Fases:
  task1  Proyecto, red, plantillas, MIGs, LB externo, firewall y prueba (pasos 1-8)
  step9  Quita el tag madrid del firewall, espera, te deja hacer capturas y lo restaura
  task2  LB interno, VM utility y prueba de carga con k6
  task3  Bucket, backend bucket y regla /picture
  task4  Autoscaling en mig-madrid, quita Asia del LB interno y repite k6
  all    Todas, en orden, con pausas para hacer capturas (por defecto)
"""
import argparse
import base64
import json
import mimetypes
import os
import random
import re
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib

# ----------------------------------------------------------------------------
# Constantes
# ----------------------------------------------------------------------------
NET = "ml-network"
CONTAINER_IMAGE = "rmartin01/practices:http-server-0.6"
APP_PORT = 8080
R_MAD = "europe-southwest1"
R_ASIA = "asia-east1"
Z_UTIL = "europe-southwest1-a"

HC = "my-health-check"
BS_EXT = "backend-service-madrid"
BS_INT = "backend-service-int"
URLMAP_EXT = "my-ext-app-lb"
URLMAP_INT = "my-int-app-lb"
PROXY_EXT = "my-ext-app-lb-proxy"
PROXY_INT = "my-int-app-lb-proxy"
FR_EXT = "my-ext-app-lb-frontend"
FR_INT = "my-int-app-lb-frontend"
IP_EXT = "my-ext-lb-ip"
FW_HTTP = "allow-http-8080"
FW_SSH = "allow-ssh"
BACKEND_BUCKET = "picture-backend-bucket"
PATH_MATCHER = "picture-matcher"
VM_UTIL = "utility"

MIG_MAD = "mig-madrid"
MIG_ASIA = "mig-asia"
TEMPLATES = [
    ("my-template-madrid", "madrid"),
    ("my-template-asia", "asia"),
]

CONSOLE = "https://console.cloud.google.com"
STATE_FILE = os.path.expanduser("~/.lab3_project_id")

K6_JS = """import http from 'k6/http';
import { sleep } from 'k6';
let ip = __ENV.IP;
export const options = {
  stages: [
    { duration: '30s', target: 10 },
    { duration: '1m30s', target: 200 },
    { duration: '20s', target: 0 },
  ]
};
export default function () {
  http.get('http://' + ip + '/test');
  sleep(1);
}
"""

# Startup script de las plantillas: Container-Optimized OS ya trae Docker.
# - Abre el puerto de la app en el iptables del host (COS bloquea entrada por defecto
#   cuando el contenedor usa --network host).
# - Lanza el contenedor en la red del host (como hacía el antiguo agente de contenedores),
#   así la app escucha directamente en el puerto 8080.
STARTUP_SCRIPT = """#!/bin/bash
export HOME=/tmp
iptables -w -A INPUT -p tcp --dport %d -j ACCEPT
for i in $(seq 1 10); do
  docker rm -f app 2>/dev/null
  docker run -d --name app --restart=always --network host %s && break
  sleep 15
done
""" % (APP_PORT, CONTAINER_IMAGE)

PROJECT = None
NO_PAUSE = False
ARGS = None

# En Windows gcloud es "gcloud.cmd": hay que usar la ruta completa (subprocess no resuelve .cmd por nombre)
GCLOUD = shutil.which("gcloud") or "gcloud"
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # noqa
        pass


class Fail(Exception):
    pass


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------
def banner(text):
    print("\n" + "=" * 78)
    print(text)
    print("=" * 78)


def info(text):
    print("[INFO] " + text)


def warn(text):
    print("[AVISO] " + text)


def ok(text):
    print("[OK] " + text)


def fmt(cmd):
    return " ".join(shlex.quote(c) for c in cmd)


def pause(msg):
    if NO_PAUSE or not sys.stdin.isatty():
        return
    input("\n>>> " + msg + "\n    Pulsa ENTER para continuar... ")


def g(args):
    """Construye un comando gcloud con proyecto y sin preguntas."""
    return [GCLOUD] + list(args) + ["--project=" + PROJECT, "--quiet"]


def sh(cmd, retries=1, delay=15, ok_if_exists=True, check=True):
    """Ejecuta un comando mostrando salida. Reintenta y tolera 'already exists'."""
    last = None
    for attempt in range(1, retries + 1):
        print("\n$ " + fmt(cmd))
        p = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if p.returncode == 0:
            if out:
                print(out)
            if err:
                print(err)
            return p
        if ok_if_exists and "already exists" in (err + out).lower():
            print("  (ya existía, se continúa)")
            return p
        if out:
            print(out)
        if err:
            print(err)
        last = p
        # Errores que no se arreglan reintentando
        low = (err + out).lower()
        if "discontinued" in low or "deprecated and removed" in low:
            break
        if attempt < retries:
            print("  Reintentando en %ss (%d/%d)..." % (delay, attempt, retries))
            time.sleep(delay)
    if check:
        raise Fail("Falló el comando: " + fmt(cmd))
    return last


def q(args):
    """Ejecuta gcloud en silencio y devuelve (rc, stdout, stderr)."""
    p = subprocess.run(g(args), capture_output=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def raw(cmd):
    p = subprocess.run(cmd + ["--quiet"], capture_output=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def exists(args):
    return q(args)[0] == 0


def jq(args):
    rc, out, _ = q(list(args) + ["--format=json"])
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out)
    except ValueError:
        return None


def short(url):
    return (url or "").rstrip("/").split("/")[-1]


def tips(items):
    print("\n--- DÓNDE HACER LAS CAPTURAS ---")
    for desc, path in items:
        print("  * %s\n      %s/%s?project=%s" % (desc, CONSOLE, path, PROJECT))
    print("  (Si el enlace no abre la sección exacta, usa el menú de la consola.)")


def http_get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read(300)
    except urllib.error.HTTPError as e:
        return e.code, "", b""
    except Exception as e:  # noqa
        return 0, "", str(e).encode()


def wait_http(url, timeout=900, want_image=False):
    end = time.time() + timeout
    while time.time() < end:
        st, ct, _ = http_get(url)
        print("  GET %s -> %s %s" % (url, st, ct))
        if st == 200 and (not want_image or ct.startswith("image/")):
            return True
        time.sleep(20)
    warn("Sin respuesta correcta de %s tras %ds. Puede ser propagación: reintenta en unos minutos." % (url, timeout))
    return False


def probe(url, n=12):
    seen = {}
    for _ in range(n):
        st, _, body = http_get(url)
        key = "%s %s" % (st, body.decode(errors="replace").strip()[:120])
        seen[key] = seen.get(key, 0) + 1
        time.sleep(0.5)
    print("\nRespuestas de %s (%d peticiones):" % (url, n))
    for k, v in seen.items():
        print("  %3d x %s" % (v, k))


def make_png(path, w=640, h=360):
    rows = []
    for y in range(h):
        row = bytearray([0])
        for x in range(w):
            row += bytes((int(255 * x / w), int(255 * y / h), 180))
        rows.append(bytes(row))
    raw_data = b"".join(rows)

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw_data, 9))
           + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def startup_script_file():
    """Escribe el startup script a un fichero temporal (LF) para --metadata-from-file.
    Pasarlo por línea de comandos a gcloud.cmd en Windows rompe comillas y $."""
    path = os.path.join(tempfile.gettempdir(), "lab3_startup.sh")
    with open(path, "w", newline="\n") as f:
        f.write(STARTUP_SCRIPT)
    return path


# ----------------------------------------------------------------------------
# Salud de backends
# ----------------------------------------------------------------------------
def health(service):
    data = jq(["compute", "backend-services", "get-health", service, "--global"])
    res = {}
    for item in data or []:
        grp = short(item.get("backend", ""))
        statuses = (item.get("status") or {}).get("healthStatus") or []
        res[grp] = [s.get("healthState") for s in statuses]
    return res


def health_summary(h):
    return {k: "%d/%d HEALTHY" % (v.count("HEALTHY"), len(v)) for k, v in h.items()}


def wait_healthy(service, groups, timeout=900):
    end = time.time() + timeout
    while time.time() < end:
        h = health(service)
        print("  Salud de %s: %s" % (service, health_summary(h)))
        if all(h.get(grp) and all(s == "HEALTHY" for s in h[grp]) for grp in groups):
            ok("Todos los backends de %s están Healthy" % service)
            return True
        time.sleep(20)
    warn("No todos los backends de %s están Healthy tras %ds. Revisa el firewall y reintenta." % (service, timeout))
    return False


# ----------------------------------------------------------------------------
# Preparación: proyecto, facturación, APIs, red
# ----------------------------------------------------------------------------
def private_subnet(region):
    data = jq(["compute", "networks", "subnets", "list", "--network=" + NET, "--regions=" + region]) or []
    for s in data:
        if not s.get("network", "").endswith("/" + NET):
            continue
        if s.get("purpose") in (None, "PRIVATE", "PRIVATE_RFC_1918"):
            return s["name"]
    return None


def valid_project_id(pid):
    return bool(re.match(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$", pid or ""))


def project_exists(pid):
    return raw([GCLOUD, "projects", "describe", pid])[0] == 0


def ensure_project():
    """Crea el proyecto si no existe. Nunca reutiliza el proyecto por defecto de Cloud Shell."""
    global PROJECT
    saved = None
    if os.path.isfile(STATE_FILE):
        with open(STATE_FILE) as f:
            saved = f.read().strip() or None
    pid = ARGS.project_id or saved
    generated = False
    if not pid:
        pid = "lab-3-a-%d" % random.randint(10000, 99999)
        generated = True
    if not valid_project_id(pid):
        raise Fail("ID de proyecto inválido: %s (6-30 caracteres, minúsculas, números y guiones)." % pid)

    for _ in range(8):
        if project_exists(pid):
            ok("El proyecto %s ya existe y es accesible" % pid)
            break
        info("Creando el proyecto %s (nombre visible: lab-3-a)..." % pid)
        cmd = [GCLOUD, "projects", "create", pid, "--name=lab-3-a", "--quiet"]
        if ARGS.organization:
            cmd.append("--organization=" + ARGS.organization)
        if ARGS.folder:
            cmd.append("--folder=" + ARGS.folder)
        print("\n$ " + fmt(cmd))
        p = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
        text = ((p.stdout or "") + "\n" + (p.stderr or "")).strip()
        print(text)
        if p.returncode == 0:
            for _ in range(24):
                if project_exists(pid):
                    break
                time.sleep(5)
            else:
                raise Fail("El proyecto se creó pero aún no es accesible. Reintenta en un minuto.")
            ok("Proyecto %s creado" % pid)
            break
        low = text.lower()
        if "already in use" in low or "already exists" in low:
            if generated or not ARGS.project_id:
                pid = "lab-3-a-%d" % random.randint(10000, 99999)
                generated = True
                warn("ID ocupado. Probando con otro: " + pid)
                continue
            raise Fail("El ID %s ya lo usa otro proyecto. Elige otro --project-id o ejecuta sin --project-id "
                       "para que se genere uno automáticamente." % pid)
        hints = []
        if "permission" in low or "denied" in low or "forbidden" in low:
            hints.append("Tu cuenta no tiene permiso para crear proyectos (típico de cuentas de organización). "
                         "Prueba con --organization <ID> o --folder <ID>, o crea el proyecto a mano en la consola "
                         "y pásalo con --project-id.")
        if "terms" in low or "tos" in low:
            hints.append("Acepta los términos de servicio entrando una vez en console.cloud.google.com.")
        if "quota" in low or "limit" in low:
            hints.append("Has alcanzado el límite de proyectos de tu cuenta: borra alguno que no uses.")
        if "api" in low and "enable" in low:
            hints.append("Habilita la API Cloud Resource Manager: gcloud services enable cloudresourcemanager.googleapis.com")
        raise Fail("No se pudo crear el proyecto.\n  " + ("\n  ".join(hints) if hints else
                   "Revisa el mensaje de error de arriba."))
    else:
        raise Fail("No se pudo crear un proyecto tras varios intentos.")

    PROJECT = pid
    with open(STATE_FILE, "w") as f:
        f.write(pid)
    ok("ID del proyecto: %s (guardado en %s para las siguientes ejecuciones)" % (pid, STATE_FILE))
    sh([GCLOUD, "config", "set", "project", PROJECT, "--quiet"])


def prepare():
    global PROJECT
    banner("PASO 0: Preparación (proyecto, facturación, APIs y red)")
    if not shutil.which("gcloud"):
        raise Fail("No se encuentra gcloud. Usa Cloud Shell.")
    _, aout, _ = raw([GCLOUD, "auth", "list", "--filter=status:ACTIVE", "--format=json"])
    acct = ""
    try:
        adata = json.loads(aout) if aout else []
        acct = adata[0].get("account", "") if adata else ""
    except ValueError:
        pass
    if not acct:
        raise Fail("No hay sesión activa. Ejecuta: gcloud auth login")
    info("Cuenta activa: " + acct)

    ensure_project()

    _, bout, _ = raw([GCLOUD, "billing", "projects", "describe", PROJECT, "--format=json"])
    try:
        billing_on = bool(json.loads(bout).get("billingEnabled")) if bout else False
    except ValueError:
        billing_on = False
    if not billing_on:
        acc = ARGS.billing_account
        if not acc:
            _, out, _ = raw([GCLOUD, "billing", "accounts", "list", "--format=json"])
            try:
                accs = [a.get("name", "").split("/")[-1] for a in json.loads(out) if a.get("open")] if out else []
            except ValueError:
                accs = []
            if len(accs) == 1:
                acc = accs[0]
            elif not accs:
                raise Fail("No hay cuentas de facturación activas. Activa tu crédito/facturación en la consola.")
            else:
                raise Fail("Hay varias cuentas de facturación (%s). Usa --billing-account ID." % ", ".join(accs))
        sh([GCLOUD, "billing", "projects", "link", PROJECT, "--billing-account=" + acc, "--quiet"])
    else:
        ok("La facturación ya está activa")

    sh(g(["services", "enable", "compute.googleapis.com", "monitoring.googleapis.com",
          "storage.googleapis.com"]), retries=3)
    info("Esperando a que la API de Compute esté lista...")
    for _ in range(20):
        if q(["compute", "networks", "list", "--limit=1"])[0] == 0:
            break
        time.sleep(15)
    else:
        raise Fail("La API de Compute Engine no responde. Reintenta en unos minutos.")

    net = jq(["compute", "networks", "describe", NET])
    if net is None:
        sh(g(["compute", "networks", "create", NET, "--subnet-mode=auto"]), retries=3)
        net = jq(["compute", "networks", "describe", NET])
    if not net:
        raise Fail("No se pudo crear/leer la red " + NET)
    if not net.get("autoCreateSubnetworks"):
        raise Fail("La red %s existe pero NO es modo auto. Las plantillas Global necesitan red auto: "
                   "usa un proyecto nuevo (lab-3-a) o borra esa red." % NET)
    for _ in range(12):
        if private_subnet(R_MAD) and private_subnet(R_ASIA):
            break
        time.sleep(10)
    else:
        raise Fail("La red no tiene subred en %s y %s." % (R_MAD, R_ASIA))
    ok("Red %s lista con subredes en %s y %s" % (NET, R_MAD, R_ASIA))

    tips([
        ("Proyecto (selector superior o Configuración)", "iam-admin/settings"),
        ("Red ml-network > pestaña Subredes (europe-southwest1 y asia-east1)", "networking/networks/list"),
    ])


# ----------------------------------------------------------------------------
# Piezas reutilizables
# ----------------------------------------------------------------------------
def ensure_backend(service, mig, region):
    bs = jq(["compute", "backend-services", "describe", service, "--global"]) or {}
    groups = [short(b.get("group", "")) for b in (bs.get("backends") or [])]
    if mig in groups:
        ok("%s ya tiene el backend %s" % (service, mig))
        return
    sh(g(["compute", "backend-services", "add-backend", service, "--global",
          "--instance-group=" + mig, "--instance-group-region=" + region,
          "--balancing-mode=UTILIZATION", "--max-utilization=0.8"]), retries=3)


def require_infra():
    needed = [
        (["compute", "instance-groups", "managed", "describe", MIG_MAD, "--region=" + R_MAD], MIG_MAD),
        (["compute", "instance-groups", "managed", "describe", MIG_ASIA, "--region=" + R_ASIA], MIG_ASIA),
        (["compute", "backend-services", "describe", BS_EXT, "--global"], BS_EXT),
    ]
    for args, name in needed:
        if not exists(args):
            raise Fail("Falta %s. Ejecuta antes la fase task1." % name)


def ext_ip():
    out = (jq(["compute", "addresses", "describe", IP_EXT, "--global"]) or {}).get("address")
    if not out:
        raise Fail("No existe la IP del balanceador externo. Ejecuta antes la fase task1.")
    return out


def count_instances(mig, region):
    data = jq(["compute", "instance-groups", "managed", "list-instances", mig, "--region=" + region])
    return len(data) if data is not None else "?"


def int_ip():
    out = (jq(["compute", "forwarding-rules", "describe", FR_INT, "--global"]) or {}).get("IPAddress")
    if not out:
        raise Fail("No existe el balanceador interno. Ejecuta antes la fase task2.")
    return out


# ----------------------------------------------------------------------------
# TASK 1
# ----------------------------------------------------------------------------
def task1():
    banner("TASK 1: Plantillas, MIGs, LB externo, firewall y prueba")

    # Pasos 1 y 2: plantillas con COS + startup script que lanza el contenedor
    for name, tag in TEMPLATES:
        if exists(["compute", "instance-templates", "describe", name]):
            ok("Plantilla %s ya existe" % name)
        else:
            sh(g(["compute", "instance-templates", "create", name,
                  "--machine-type=e2-micro",
                  "--image-family=cos-stable", "--image-project=cos-cloud",
                  "--tags=" + tag, "--network=" + NET,
                  "--metadata-from-file=startup-script=" + startup_script_file()]), retries=3)

    # Pasos 3 y 4
    for mig, region, tpl in [(MIG_MAD, R_MAD, "my-template-madrid"), (MIG_ASIA, R_ASIA, "my-template-asia")]:
        if exists(["compute", "instance-groups", "managed", "describe", mig, "--region=" + region]):
            ok("MIG %s ya existe" % mig)
        else:
            sh(g(["compute", "instance-groups", "managed", "create", mig, "--template=" + tpl,
                  "--size=2", "--region=" + region]), retries=3)
        sh(g(["compute", "instance-groups", "managed", "set-named-ports", mig,
              "--named-ports=http8080:8080", "--region=" + region]), retries=3)
    for mig, region in [(MIG_MAD, R_MAD), (MIG_ASIA, R_ASIA)]:
        info("Esperando a que %s esté estable (instancias creadas)..." % mig)
        p = sh(g(["compute", "instance-groups", "managed", "wait-until", mig, "--stable",
                  "--region=" + region, "--timeout=900"]), check=False)
        if p is None or p.returncode != 0:
            warn("%s tarda en estabilizarse; se continúa." % mig)

    # Pasos 5 y 6: health check + backend service + backends
    if exists(["compute", "health-checks", "describe", HC, "--global"]):
        ok("Health check ya existe")
    else:
        sh(g(["compute", "health-checks", "create", "tcp", HC, "--port=8080", "--global"]), retries=3)

    if exists(["compute", "backend-services", "describe", BS_EXT, "--global"]):
        ok("Backend service %s ya existe" % BS_EXT)
    else:
        sh(g(["compute", "backend-services", "create", BS_EXT, "--load-balancing-scheme=EXTERNAL_MANAGED",
              "--protocol=HTTP", "--port-name=http8080", "--health-checks=" + HC, "--global"]), retries=3)
    ensure_backend(BS_EXT, MIG_MAD, R_MAD)
    ensure_backend(BS_EXT, MIG_ASIA, R_ASIA)

    # Frontend
    if not exists(["compute", "addresses", "describe", IP_EXT, "--global"]):
        sh(g(["compute", "addresses", "create", IP_EXT, "--ip-version=IPV4",
              "--network-tier=PREMIUM", "--global"]), retries=3)
    if not exists(["compute", "url-maps", "describe", URLMAP_EXT, "--global"]):
        sh(g(["compute", "url-maps", "create", URLMAP_EXT, "--default-service=" + BS_EXT, "--global"]), retries=3)
    if not exists(["compute", "target-http-proxies", "describe", PROXY_EXT, "--global"]):
        sh(g(["compute", "target-http-proxies", "create", PROXY_EXT, "--url-map=" + URLMAP_EXT,
              "--global"]), retries=3)
    if not exists(["compute", "forwarding-rules", "describe", FR_EXT, "--global"]):
        sh(g(["compute", "forwarding-rules", "create", FR_EXT, "--load-balancing-scheme=EXTERNAL_MANAGED",
              "--network-tier=PREMIUM", "--address=" + IP_EXT, "--global",
              "--target-http-proxy=" + PROXY_EXT, "--ports=80"]), retries=3)
    ip = ext_ip()
    ok("IP del balanceador externo: " + ip)

    # Paso 7: firewall
    if exists(["compute", "firewall-rules", "describe", FW_HTTP]):
        sh(g(["compute", "firewall-rules", "update", FW_HTTP, "--target-tags=madrid,asia"]), retries=3)
    else:
        sh(g(["compute", "firewall-rules", "create", FW_HTTP, "--network=" + NET, "--direction=INGRESS",
              "--action=allow", "--rules=tcp:8080", "--source-ranges=0.0.0.0/0",
              "--target-tags=madrid,asia"]), retries=3)
    if not exists(["compute", "firewall-rules", "describe", FW_SSH]):
        sh(g(["compute", "firewall-rules", "create", FW_SSH, "--network=" + NET, "--direction=INGRESS",
              "--action=allow", "--rules=tcp:22", "--source-ranges=0.0.0.0/0"]), retries=3)

    # Paso 8
    info("Esperando a que los backends estén Healthy (puede tardar varios minutos: arranque + descarga de la imagen)...")
    wait_healthy(BS_EXT, [MIG_MAD, MIG_ASIA])
    info("Esperando a que el balanceador responda en http://%s/test ..." % ip)
    if wait_http("http://%s/test" % ip):
        probe("http://%s/test" % ip)
        info("Nota: desde Cloud Shell el 'backend más cercano' depende de la región de Cloud Shell. "
             "Para la captura del paso 8 usa tu NAVEGADOR local: http://%s/test" % ip)

    print("")
    for cmd in (["compute", "instance-templates", "list"],
                ["compute", "instance-groups", "managed", "list"],
                ["compute", "instances", "list"],
                ["compute", "firewall-rules", "list", "--filter=network~" + NET]):
        sh(g(cmd), check=False)

    tips([
        ("Plantillas de instancia (Global, tags y contenedor)", "compute/instanceTemplates/list"),
        ("Grupos de instancias (2 MIG, autoscaling Off)", "compute/instanceGroups/list"),
        ("Instancias de VM (4 VMs)", "compute/instances"),
        ("Firewall allow-http-8080 (puerto 8080, tags madrid y asia)", "networking/firewalls/list"),
        ("Balanceo de carga > my-ext-app-lb (frontend, backends Healthy)", "net-services/loadbalancing/list/loadBalancers"),
        ("Health check my-health-check", "compute/healthChecks"),
        ("Navegador local: http://%s/test (recarga varias veces)" % ip, "compute/instances"),
    ])


# ----------------------------------------------------------------------------
# PASO 9 (Task 1): quitar el tag madrid del firewall
# ----------------------------------------------------------------------------
def step9():
    banner("PASO 9 (Task 1): quitar el tag madrid del firewall")
    require_infra()
    ip = ext_ip()
    sh(g(["compute", "firewall-rules", "update", FW_HTTP, "--target-tags=asia"]), retries=3)
    try:
        info("Esperando a que mig-madrid pase a UNHEALTHY (hasta 6 min)...")
        end = time.time() + 360
        while time.time() < end:
            h = health(BS_EXT)
            print("  Salud: %s" % health_summary(h))
            if h.get(MIG_MAD) and "HEALTHY" not in h[MIG_MAD]:
                ok("mig-madrid está Unhealthy: todo el tráfico va a Asia")
                break
            time.sleep(15)
        else:
            warn("Madrid no llegó a marcarse Unhealthy en 6 min.")
        probe("http://%s/test" % ip)
        tips([
            ("Balanceo de carga > my-ext-app-lb: mig-madrid en rojo y mig-asia en verde", "net-services/loadbalancing/list/loadBalancers"),
            ("Firewall allow-http-8080 con solo el tag asia", "networking/firewalls/list"),
            ("Navegador local: http://%s/test (solo instancias de Asia)" % ip, "compute/instances"),
        ])
        pause("Haz ahora las capturas del paso 9. Después se RESTAURARÁ la regla (madrid + asia).")
    finally:
        info("Restaurando la regla de firewall (madrid + asia)")
        sh(g(["compute", "firewall-rules", "update", FW_HTTP, "--target-tags=madrid,asia"]), retries=5)
    wait_healthy(BS_EXT, [MIG_MAD, MIG_ASIA])


# ----------------------------------------------------------------------------
# k6 en la VM utility
# ----------------------------------------------------------------------------
def ssh_cmd(remote):
    return [GCLOUD, "compute", "ssh", VM_UTIL, "--zone=" + Z_UTIL, "--project=" + PROJECT, "--quiet",
            "--strict-host-key-checking=no", "--command=" + remote]


def ssh_q(remote):
    p = subprocess.run(ssh_cmd(remote), capture_output=True, encoding="utf-8", errors="replace")
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def wait_ssh():
    info("Esperando a poder entrar por SSH a la VM utility y a que Docker esté listo...")
    for i in range(30):
        rc, out, _ = ssh_q("docker --version")
        if rc == 0:
            ok("SSH y Docker listos: " + out)
            return
        print("  SSH aún no disponible (%d/30)..." % (i + 1))
        time.sleep(15)
    raise Fail("No se pudo entrar por SSH a utility. Revisa el firewall allow-ssh.")


def push_k6_script():
    b64 = base64.b64encode(K6_JS.encode()).decode()
    rc, out, err = ssh_q("echo %s | base64 -d > test.js && cat test.js" % b64)
    if rc != 0:
        raise Fail("No se pudo crear test.js en utility: " + err)
    ok("test.js creado en la VM utility")


def wait_ilb_from_vm(ip, timeout=900):
    end = time.time() + timeout
    while time.time() < end:
        rc, out, _ = ssh_q("curl -sf -o /dev/null -m 8 http://%s/test && echo 200" % ip)
        print("  Desde utility: GET http://%s/test -> %s" % (ip, out))
        if rc == 0 and out == "200":
            return True
        time.sleep(20)
    warn("El balanceador interno aún no responde 200 desde utility; se lanza k6 igualmente.")
    return False


def run_k6(ip, mig=None, region=None):
    remote = "docker run --rm --env IP=%s -v $PWD/test.js:/scripts/test.js grafana/k6 run /scripts/test.js" % ip
    info("Lanzando k6 (dura ~2 min 20 s, más la descarga de la imagen)...")
    proc = subprocess.Popen(ssh_cmd(remote))
    last = 0.0
    while proc.poll() is None:
        time.sleep(3)
        if mig and time.time() - last > 25:
            last = time.time()
            print("\n  [monitor] Instancias en %s: %s" % (mig, count_instances(mig, region)))
    if proc.returncode != 0:
        warn("k6 terminó con código %s" % proc.returncode)
    else:
        ok("k6 terminado")


def ensure_utility():
    if exists(["compute", "instances", "describe", VM_UTIL, "--zone=" + Z_UTIL]):
        ok("VM utility ya existe")
    else:
        sh(g(["compute", "instances", "create", VM_UTIL, "--zone=" + Z_UTIL, "--machine-type=e2-standard-4",
              "--image-family=cos-stable", "--image-project=cos-cloud", "--network=" + NET]), retries=3)
    wait_ssh()
    push_k6_script()


# ----------------------------------------------------------------------------
# TASK 2
# ----------------------------------------------------------------------------
def ensure_proxy_subnet(name, region, rng):
    data = jq(["compute", "networks", "subnets", "list", "--network=" + NET, "--regions=" + region]) or []
    for s in data:
        if s.get("network", "").endswith("/" + NET) and s.get("purpose") == "GLOBAL_MANAGED_PROXY":
            ok("Ya hay subred proxy-only en %s (%s)" % (region, s["name"]))
            return
    sh(g(["compute", "networks", "subnets", "create", name, "--purpose=GLOBAL_MANAGED_PROXY",
          "--role=ACTIVE", "--region=" + region, "--network=" + NET, "--range=" + rng]), retries=3)


def task2():
    banner("TASK 2: Balanceador interno (cross-region) y prueba de carga con k6")
    require_infra()

    ensure_proxy_subnet("proxy-only-madrid", R_MAD, "172.16.0.0/23")
    ensure_proxy_subnet("proxy-only-asia", R_ASIA, "172.16.2.0/23")

    if exists(["compute", "backend-services", "describe", BS_INT, "--global"]):
        ok("Backend service %s ya existe" % BS_INT)
    else:
        sh(g(["compute", "backend-services", "create", BS_INT, "--load-balancing-scheme=INTERNAL_MANAGED",
              "--protocol=HTTP", "--port-name=http8080", "--health-checks=" + HC, "--global"]), retries=3)
    ensure_backend(BS_INT, MIG_MAD, R_MAD)
    ensure_backend(BS_INT, MIG_ASIA, R_ASIA)

    if not exists(["compute", "url-maps", "describe", URLMAP_INT, "--global"]):
        sh(g(["compute", "url-maps", "create", URLMAP_INT, "--default-service=" + BS_INT, "--global"]), retries=3)
    if not exists(["compute", "target-http-proxies", "describe", PROXY_INT, "--global"]):
        sh(g(["compute", "target-http-proxies", "create", PROXY_INT, "--url-map=" + URLMAP_INT,
              "--global"]), retries=3)
    if not exists(["compute", "forwarding-rules", "describe", FR_INT, "--global"]):
        subnet = private_subnet(R_MAD)
        if not subnet:
            raise Fail("No se encuentra la subred de %s en %s" % (R_MAD, NET))
        sh(g(["compute", "forwarding-rules", "create", FR_INT, "--load-balancing-scheme=INTERNAL_MANAGED",
              "--network=" + NET, "--subnet=" + subnet, "--subnet-region=" + R_MAD, "--ports=80",
              "--target-http-proxy=" + PROXY_INT, "--global-target-http-proxy", "--global"]), retries=3)
    ip = int_ip()
    ok("IP del balanceador interno: " + ip)

    wait_healthy(BS_INT, [MIG_MAD, MIG_ASIA])
    ensure_utility()
    wait_ilb_from_vm(ip)

    run_k6(ip)

    tips([
        ("Balanceo de carga: my-ext-app-lb (externo) y my-int-app-lb (interno)", "net-services/loadbalancing/list/loadBalancers"),
        ("Red ml-network > Subredes: proxy-only-madrid y proxy-only-asia", "networking/networks/list"),
        ("VM utility en europe-southwest1-a", "compute/instances"),
        ("Monitoring > Metrics Explorer: VM Instance > CPU utilization, agrupada por instance_name", "monitoring/metrics-explorer"),
        ("Balanceo de carga > my-int-app-lb > pestaña Supervisión", "net-services/loadbalancing/list/loadBalancers"),
    ])
    pause("Haz las capturas de monitorización de la Task 2 (las métricas tardan 1-2 min en aparecer).")


# ----------------------------------------------------------------------------
# TASK 3
# ----------------------------------------------------------------------------
def pick_image():
    path = ARGS.image
    if path:
        path = os.path.expanduser(path)
        if not os.path.isfile(path):
            warn("No se encuentra %s; se generará una imagen de prueba." % path)
            path = None
    if not path:
        path = os.path.expanduser("~/lab3_picture.png")
        make_png(path)
        info("Imagen de prueba generada en " + path)
    ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return path, ctype


def task3():
    banner("TASK 3: Bucket, backend bucket y regla de ruta /picture")
    require_infra()
    bucket = PROJECT + "-picture"

    if exists(["storage", "buckets", "describe", "gs://" + bucket]):
        ok("Bucket %s ya existe" % bucket)
    else:
        sh(g(["storage", "buckets", "create", "gs://" + bucket, "--location=" + R_MAD,
              "--uniform-bucket-level-access"]), retries=3)

    path, ctype = pick_image()
    # El LB busca un objeto con el nombre de la ruta: /picture -> objeto "picture"
    sh(g(["storage", "cp", path, "gs://%s/picture" % bucket, "--content-type=" + ctype]), retries=3)
    p = sh(g(["storage", "buckets", "add-iam-policy-binding", "gs://" + bucket,
              "--member=allUsers", "--role=roles/storage.objectViewer"]), retries=3, check=False)
    if p is None or p.returncode != 0:
        warn("No se pudo hacer público el bucket (¿política de organización?). /picture daría 403.")

    if exists(["compute", "backend-buckets", "describe", BACKEND_BUCKET]):
        ok("Backend bucket ya existe")
    else:
        sh(g(["compute", "backend-buckets", "create", BACKEND_BUCKET, "--gcs-bucket-name=" + bucket]), retries=3)

    um = jq(["compute", "url-maps", "describe", URLMAP_EXT, "--global"]) or {}
    matchers = [m.get("name") for m in (um.get("pathMatchers") or [])]
    if PATH_MATCHER in matchers:
        ok("La regla de ruta /picture ya existe")
    else:
        sh(g(["compute", "url-maps", "add-path-matcher", URLMAP_EXT, "--path-matcher-name=" + PATH_MATCHER,
              "--default-service=" + BS_EXT, "--backend-bucket-path-rules=/picture=" + BACKEND_BUCKET,
              "--new-hosts=*", "--global"]), retries=3)
    sh(g(["compute", "url-maps", "describe", URLMAP_EXT, "--global"]), check=False)

    ip = ext_ip()
    info("Esperando a que /picture sirva la imagen (propagación del URL map)...")
    wait_http("http://%s/picture" % ip, want_image=True)
    wait_http("http://%s/test" % ip)

    tips([
        ("Cloud Storage > bucket %s con el objeto 'picture' y acceso público" % bucket, "storage/browser"),
        ("Balanceo de carga > my-ext-app-lb > Reglas de host y ruta (/picture -> backend bucket)", "net-services/loadbalancing/list/loadBalancers"),
        ("Navegador local: http://%s/test y http://%s/picture (dos pestañas)" % (ip, ip), "storage/browser"),
    ])
    pause("Haz las capturas de la Task 3 (reglas de ruta y las dos URLs en el navegador).")


# ----------------------------------------------------------------------------
# TASK 4
# ----------------------------------------------------------------------------
def task4():
    banner("TASK 4: Autoscaling en mig-madrid y nueva prueba de carga")
    require_infra()
    ip = int_ip()

    bs = jq(["compute", "backend-services", "describe", BS_INT, "--global"]) or {}
    groups = [short(b.get("group", "")) for b in (bs.get("backends") or [])]
    if MIG_ASIA in groups:
        sh(g(["compute", "backend-services", "remove-backend", BS_INT, "--global",
              "--instance-group=" + MIG_ASIA, "--instance-group-region=" + R_ASIA]), retries=3)
    else:
        ok("mig-asia ya no está en el balanceador interno")
    ensure_backend(BS_INT, MIG_MAD, R_MAD)

    sh(g(["compute", "instance-groups", "managed", "set-autoscaling", MIG_MAD, "--region=" + R_MAD,
          "--min-num-replicas=2", "--max-num-replicas=5", "--target-cpu-utilization=0.6",
          "--cool-down-period=60"]), retries=3)
    sh(g(["compute", "instance-groups", "managed", "describe", MIG_MAD, "--region=" + R_MAD]), check=False)
    info("Parámetro que controla el autoscaling: target CPU utilization (0.6 = 60%).")

    wait_healthy(BS_INT, [MIG_MAD])
    ensure_utility()
    wait_ilb_from_vm(ip)

    run_k6(ip, MIG_MAD, R_MAD)

    info("Vigilando el tamaño del grupo 2 minutos más tras la prueba...")
    for _ in range(4):
        print("  [monitor] Instancias en %s: %s" % (MIG_MAD, count_instances(MIG_MAD, R_MAD)))
        time.sleep(30)
    sh(g(["compute", "instance-groups", "managed", "list-instances", MIG_MAD, "--region=" + R_MAD]), check=False)

    tips([
        ("Grupos de instancias > mig-madrid: autoscaling On, mín 2, máx 5, CPU 60%", "compute/instanceGroups/list"),
        ("mig-madrid > pestaña Supervisión: nº de instancias y CPU durante la prueba", "compute/instanceGroups/list"),
        ("Instancias de VM: más de 2 VMs de mig-madrid (hasta 5)", "compute/instances"),
        ("Balanceo de carga > my-int-app-lb: solo el MIG de Madrid", "net-services/loadbalancing/list/loadBalancers"),
        ("Monitoring > Metrics Explorer: CPU utilization por instancia", "monitoring/metrics-explorer"),
    ])


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------
def main():
    global NO_PAUSE, ARGS
    ap = argparse.ArgumentParser(description="Despliega la Lab Practice 3 (no borra nada).")
    ap.add_argument("--project-id", help="ID del proyecto (opcional). Si lo omites se crea uno nuevo lab-3-a-NNNNN y se recuerda")
    ap.add_argument("--organization", help="ID de organización donde crear el proyecto (solo si tu cuenta lo exige)")
    ap.add_argument("--folder", help="ID de carpeta donde crear el proyecto (solo si tu cuenta lo exige)")
    ap.add_argument("--billing-account", help="ID de la cuenta de facturación (si no se detecta sola)")
    ap.add_argument("--image", help="Ruta de tu imagen para /picture (si no, se genera una)")
    ap.add_argument("--phase", default="all", choices=["all", "task1", "step9", "task2", "task3", "task4"])
    ap.add_argument("--no-pause", action="store_true",
                    help="No pedir ENTER entre fases (perderás el momento de capturas del paso 9)")
    ARGS = ap.parse_args()
    NO_PAUSE = ARGS.no_pause

    phases = {"task1": task1, "step9": step9, "task2": task2, "task3": task3, "task4": task4}
    order = ["task1", "step9", "task2", "task3", "task4"] if ARGS.phase == "all" else [ARGS.phase]
    try:
        prepare()
        for i, ph in enumerate(order):
            phases[ph]()
            if ARGS.phase == "all" and i < len(order) - 1:
                pause("Fase %s terminada. Haz las capturas pendientes." % ph)
        banner("TERMINADO. No se ha borrado nada. Para limpiar: python3 lab3_cleanup.py --project-id " + PROJECT)
    except Fail as e:
        print("\n[ERROR] %s" % e)
        print("Puedes relanzar el script: salta lo que ya está creado.")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nInterrumpido. Puedes relanzar el script; continúa donde lo dejó.")
        sys.exit(130)


if __name__ == "__main__":
    main()