#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Lab Practice 3 - LIMPIEZA

Elimina los recursos creados por lab3_deploy.py, en el orden correcto de dependencias.
Ejecútalo SOLO cuando hayas hecho todas las capturas.

Uso:
  python3 lab3_cleanup.py                                          # usa el proyecto guardado por el deploy
  python3 lab3_cleanup.py --project-id lab-3-a-48213              # borra los recursos, conserva el proyecto
  python3 lab3_cleanup.py --project-id lab-3-a-48213 --delete-project   # borra el proyecto entero
  Añade --yes para no pedir confirmación.

Es idempotente: si algo ya no existe lo salta, y puedes relanzarlo si algún borrado falla.
"""
import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import time

NET = "ml-network"
R_MAD = "europe-southwest1"
R_ASIA = "asia-east1"
Z_UTIL = "europe-southwest1-a"

PROJECT = None

# En Windows gcloud es "gcloud.cmd": hay que usar la ruta completa
GCLOUD = shutil.which("gcloud") or "gcloud"
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:  # noqa
        pass


def fmt(cmd):
    return " ".join(shlex.quote(c) for c in cmd)


def g(args):
    return [GCLOUD] + list(args) + ["--project=" + PROJECT, "--quiet"]


def exists(args):
    p = subprocess.run(g(args), capture_output=True, encoding="utf-8", errors="replace")
    return p.returncode == 0


def run(cmd, retries=1, delay=20):
    """Devuelve True si el comando termina bien."""
    for attempt in range(1, retries + 1):
        print("\n$ " + fmt(cmd))
        p = subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace")
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if out:
            print(out)
        if err:
            print(err)
        if p.returncode == 0:
            return True
        if attempt < retries:
            print("  Reintentando en %ss (%d/%d)..." % (delay, attempt, retries))
            time.sleep(delay)
    return False


FAILED = []


def delete(label, describe, delete_cmd, retries=3, delay=20):
    if not exists(describe):
        print("[SALTADO] %s: no existe" % label)
        return
    print("\n[BORRANDO] " + label)
    if run(g(delete_cmd), retries=retries, delay=delay):
        print("[OK] %s eliminado" % label)
    else:
        print("[ERROR] No se pudo eliminar " + label)
        FAILED.append(label)


def main():
    global PROJECT
    ap = argparse.ArgumentParser(description="Elimina los recursos de la Lab Practice 3.")
    ap.add_argument("--project-id", help="Si lo omites, se usa el que guardó lab3_deploy.py")
    ap.add_argument("--delete-project", action="store_true",
                    help="Borra el proyecto completo (y con él todo lo que contiene)")
    ap.add_argument("--yes", action="store_true", help="No pedir confirmación")
    args = ap.parse_args()

    if not shutil.which("gcloud"):
        print("[ERROR] No se encuentra gcloud. Usa Cloud Shell.")
        sys.exit(1)
    if not args.project_id:
        state = os.path.expanduser("~/.lab3_project_id")
        if os.path.isfile(state):
            args.project_id = open(state).read().strip()
            print("Usando el proyecto guardado por lab3_deploy.py: " + args.project_id)
    if not args.project_id:
        print("[ERROR] Indica el proyecto con --project-id.")
        sys.exit(1)
    if not re.match(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$", args.project_id):
        print("[ERROR] ID de proyecto inválido.")
        sys.exit(1)
    PROJECT = args.project_id

    what = "el PROYECTO COMPLETO" if args.delete_project else "TODOS los recursos de la práctica"
    print("Se va a eliminar %s en el proyecto '%s'." % (what, PROJECT))
    if not args.yes:
        if not sys.stdin.isatty():
            print("[ERROR] Sin terminal interactivo: usa --yes.")
            sys.exit(1)
        if input("Escribe el ID del proyecto para confirmar: ").strip() != PROJECT:
            print("No coincide. Cancelado, no se ha borrado nada.")
            sys.exit(1)

    if args.delete_project:
        ok = run([GCLOUD, "projects", "delete", PROJECT, "--quiet"])
        if ok:
            print("\n[OK] Proyecto %s marcado para eliminación (se borra definitivamente en ~30 días)." % PROJECT)
            sys.exit(0)
        print("\n[ERROR] No se pudo borrar el proyecto.")
        sys.exit(1)

    bucket = PROJECT + "-picture"

    # 1. VM de pruebas
    delete("VM utility", ["compute", "instances", "describe", "utility", "--zone=" + Z_UTIL],
           ["compute", "instances", "delete", "utility", "--zone=" + Z_UTIL])

    # 2. Reglas de reenvío
    for fr in ("my-ext-app-lb-frontend", "my-int-app-lb-frontend"):
        delete("Regla de reenvío " + fr, ["compute", "forwarding-rules", "describe", fr, "--global"],
               ["compute", "forwarding-rules", "delete", fr, "--global"])

    # 3. Proxies HTTP
    for px in ("my-ext-app-lb-proxy", "my-int-app-lb-proxy"):
        delete("Proxy " + px, ["compute", "target-http-proxies", "describe", px, "--global"],
               ["compute", "target-http-proxies", "delete", px, "--global"])

    # 4. URL maps
    for um in ("my-ext-app-lb", "my-int-app-lb"):
        delete("URL map " + um, ["compute", "url-maps", "describe", um, "--global"],
               ["compute", "url-maps", "delete", um, "--global"])

    # 5. Backend services
    for bs in ("backend-service-madrid", "backend-service-int"):
        delete("Backend service " + bs, ["compute", "backend-services", "describe", bs, "--global"],
               ["compute", "backend-services", "delete", bs, "--global"])

    # 6. Backend bucket
    delete("Backend bucket picture-backend-bucket",
           ["compute", "backend-buckets", "describe", "picture-backend-bucket"],
           ["compute", "backend-buckets", "delete", "picture-backend-bucket"])

    # 7. Health check
    delete("Health check my-health-check", ["compute", "health-checks", "describe", "my-health-check", "--global"],
           ["compute", "health-checks", "delete", "my-health-check", "--global"])

    # 8. Autoscaler y MIGs (esto borra también sus instancias)
    if exists(["compute", "instance-groups", "managed", "describe", "mig-madrid", "--region=" + R_MAD]):
        run(g(["compute", "instance-groups", "managed", "stop-autoscaling", "mig-madrid", "--region=" + R_MAD]))
    for mig, region in (("mig-madrid", R_MAD), ("mig-asia", R_ASIA)):
        delete("MIG " + mig, ["compute", "instance-groups", "managed", "describe", mig, "--region=" + region],
               ["compute", "instance-groups", "managed", "delete", mig, "--region=" + region])

    # 9. Plantillas
    for tpl in ("my-template-madrid", "my-template-asia"):
        delete("Plantilla " + tpl, ["compute", "instance-templates", "describe", tpl],
               ["compute", "instance-templates", "delete", tpl])

    # 10. IP estática
    delete("IP my-ext-lb-ip", ["compute", "addresses", "describe", "my-ext-lb-ip", "--global"],
           ["compute", "addresses", "delete", "my-ext-lb-ip", "--global"])

    # 11. Firewall
    for fw in ("allow-http-8080", "allow-ssh"):
        delete("Firewall " + fw, ["compute", "firewall-rules", "describe", fw],
               ["compute", "firewall-rules", "delete", fw])

    # 12. Subredes proxy-only (pueden tardar en liberarse tras borrar los balanceadores)
    for sn, region in (("proxy-only-madrid", R_MAD), ("proxy-only-asia", R_ASIA)):
        delete("Subred " + sn, ["compute", "networks", "subnets", "describe", sn, "--region=" + region],
               ["compute", "networks", "subnets", "delete", sn, "--region=" + region], retries=8, delay=30)

    # 13. Bucket (con su contenido)
    delete("Bucket " + bucket, ["storage", "buckets", "describe", "gs://" + bucket],
           ["storage", "rm", "--recursive", "gs://" + bucket])

    # 14. Red
    delete("Red " + NET, ["compute", "networks", "describe", NET],
           ["compute", "networks", "delete", NET], retries=6, delay=30)

    print("\n" + "=" * 70)
    if FAILED:
        print("Quedaron sin borrar: " + ", ".join(FAILED))
        print("Espera 1-2 minutos y vuelve a ejecutar el script (salta lo ya eliminado).")
        sys.exit(1)
    print("LIMPIEZA COMPLETADA. El proyecto %s sigue existiendo, ahora sin recursos de la práctica." % PROJECT)
    print("Si quieres borrar también el proyecto: python3 lab3_cleanup.py --project-id %s --delete-project" % PROJECT)


if __name__ == "__main__":
    main()