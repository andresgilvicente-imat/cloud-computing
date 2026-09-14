# Task 6 - lab 1
# esto hace la tarea 3 pero con la API en vez de la consola
# uso google-cloud-compute pq es la libreria mas facil q encontre

# antes de correr esto en la terminal:
#   pip install google-cloud-compute
#   gcloud auth application-default login
#   gcloud config set project TU_PROJECT_ID
# (asi coge las credenciales solo, no hay q meter ninguna key en el codigo)

import time
from google.cloud import compute_v1

# CAMBIA ESTO por lo tuyo!!
PROJECT_ID = "modern-sublime-508206-i7"
REGION = "europe-west1"
ZONE = "europe-west1-b"
NETWORK = "ml-network"
SUBNETWORK_NAME = "ml-subnetwork" 
NETWORK_TAG = "deep"
IMAGE_NAME = "nginx"

instances_client = compute_v1.InstancesClient()
images_client = compute_v1.ImagesClient()

NETWORK_LINK = f"global/networks/{NETWORK}"
SUBNETWORK_LINK = f"regions/{REGION}/subnetworks/{SUBNETWORK_NAME}"


def crear_vm2():
    # vm2 con debian, le meto un startup-script q instala nginx solo al arrancar
    # (es lo mismo q conectarse por ssh y hacer apt install nginx a mano)
    print("creando vm2...")

    disk = compute_v1.AttachedDisk(
        auto_delete=True,
        boot=True,
        initialize_params=compute_v1.AttachedDiskInitializeParams(
            source_image="projects/debian-cloud/global/images/family/debian-12",
            disk_size_gb=10,
        ),
    )

    network_interface = compute_v1.NetworkInterface(
        network=NETWORK_LINK,
        subnetwork=SUBNETWORK_LINK,
        access_configs=[
            compute_v1.AccessConfig(
                name="External NAT",
                type_=compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name,
            )
        ],
    )

    startup_script = "#! /bin/bash\napt update\napt install -y nginx"

    instance = compute_v1.Instance(
        name="vm2",
        machine_type=f"zones/{ZONE}/machineTypes/e2-medium",
        disks=[disk],
        network_interfaces=[network_interface],
        tags=compute_v1.Tags(items=[NETWORK_TAG, "http-server"]),
        metadata=compute_v1.Metadata(
            items=[compute_v1.Items(key="startup-script", value=startup_script)]
        ),
    )

    op = instances_client.insert(project=PROJECT_ID, zone=ZONE, instance_resource=instance)
    op.result()  # esto bloquea hasta q termina, si no luego peta todo lo demas
    print("vm2 lista")


def obtener_ip(nombre_vm):
    # esto es solo pa q me diga la ip y poder mirarla en el navegador
    instance = instances_client.get(project=PROJECT_ID, zone=ZONE, instance=nombre_vm)
    ip = instance.network_interfaces[0].access_configs[0].nat_i_p
    print(f"ip de {nombre_vm}: {ip}  -> mete esto en el navegador: http://{ip}")
    return ip


def crear_imagen_desde_vm2():
    # para hacer la imagen primero hay q apagar la maquina, si no da error
    print("apagando vm2...")
    op = instances_client.stop(project=PROJECT_ID, zone=ZONE, instance="vm2")
    op.result()

    print("creando imagen...")
    image = compute_v1.Image(
        name=IMAGE_NAME,
        source_disk=f"projects/{PROJECT_ID}/zones/{ZONE}/disks/vm2",
    )
    op = images_client.insert(project=PROJECT_ID, image_resource=image)
    op.result()
    print("imagen creada")


def borrar_vm2():
    print("borrando vm2...")
    op = instances_client.delete(project=PROJECT_ID, zone=ZONE, instance="vm2")
    op.result()
    print("vm2 borrada (el disco se va solo xq puse auto_delete=True)")


def crear_vm3():
    # mismo rollo q vm2 pero cogiendo la imagen q nos hemos hecho
    print("creando vm3 desde la imagen...")

    disk = compute_v1.AttachedDisk(
        auto_delete=True,
        boot=True,
        initialize_params=compute_v1.AttachedDiskInitializeParams(
            source_image=f"projects/{PROJECT_ID}/global/images/{IMAGE_NAME}",
            disk_size_gb=10,
        ),
    )

    network_interface = compute_v1.NetworkInterface(
        network=NETWORK_LINK,
        subnetwork=SUBNETWORK_LINK,
        access_configs=[
            compute_v1.AccessConfig(
                name="External NAT",
                type_=compute_v1.AccessConfig.Type.ONE_TO_ONE_NAT.name,
            )
        ],
    )

    instance = compute_v1.Instance(
        name="vm3",
        machine_type=f"zones/{ZONE}/machineTypes/e2-medium",
        disks=[disk],
        network_interfaces=[network_interface],
        tags=compute_v1.Tags(items=[NETWORK_TAG, "http-server"]),
    )

    op = instances_client.insert(project=PROJECT_ID, zone=ZONE, instance_resource=instance)
    op.result()
    print("vm3 lista")


def limpiar_todo():
    # borramos todo menos la red, esa se queda pq la piden para las siguientes practicas
    print("borrando vm3...")
    op = instances_client.delete(project=PROJECT_ID, zone=ZONE, instance="vm3")
    op.result()

    print("borrando la imagen...")
    op = images_client.delete(project=PROJECT_ID, image=IMAGE_NAME)
    op.result()

    print("ya esta todo borrado, la red ml-network se queda")


if __name__ == "__main__":
    crear_vm2()
    print("esperando un poco a q se instale nginx solo...")
    time.sleep(90)  # 90s a ojo, si tu maquina va lenta sube esto
    obtener_ip("vm2")
    input("mira si funciona nginx en el navegador y dale a enter...")

    crear_imagen_desde_vm2()
    borrar_vm2()

    crear_vm3()
    print("esperando a q arranque vm3...")
    time.sleep(30)
    obtener_ip("vm3")
    input("mira si funciona nginx en vm3 tambien y dale a enter...")

    limpiar_todo()