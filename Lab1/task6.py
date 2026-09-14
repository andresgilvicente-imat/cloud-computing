# Task 6 - lab 1
# this repeats task 3 using the api instead of the cloud console
# we use google-cloud-compute because it was the simplest official library we found

# before running the script, use these commands in the terminal:
#   pip install google-cloud-compute
#   gcloud auth application-default login
#   gcloud config set project your_project_id
# this uses application default credentials, so no key has to be added to the code

import time
from google.cloud import compute_v1

# change this to the project you are using
PROJECT_ID = "commanding-port-508205-k5"
REGION = "europe-west1"
ZONE = "europe-west1-c"
NETWORK = "ml-network"
SUBNETWORK_NAME = "ml-subnetwork" 
NETWORK_TAG = "deep"
IMAGE_NAME = "nginx"

instances_client = compute_v1.InstancesClient()
images_client = compute_v1.ImagesClient()

NETWORK_LINK = f"global/networks/{NETWORK}"
SUBNETWORK_LINK = f"regions/{REGION}/subnetworks/{SUBNETWORK_NAME}"


def create_vm2():
    # vm2 uses debian and a startup script that installs nginx when it first boots
    # this replaces connecting through ssh and installing nginx manually
    print("\nCreating vm2...")

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
    op.result()  # wait until it finishes so the next api calls do not run too early
    print("vm2 is ready")


def get_external_ip(vm_name):
    # get the external ip so we can check the nginx page in the browser
    instance = instances_client.get(project=PROJECT_ID, zone=ZONE, instance=vm_name)
    ip = instance.network_interfaces[0].access_configs[0].nat_i_p
    print(f"IP of {vm_name}: {ip}  -> open this in the browser: http://{ip}")
    return ip


def create_image_from_vm2():
    # vm2 has to be stopped before its boot disk can be used for the image
    print("Stopping vm2...")
    op = instances_client.stop(project=PROJECT_ID, zone=ZONE, instance="vm2")
    op.result()

    print("Creating image...")
    image = compute_v1.Image(
        name=IMAGE_NAME,
        source_disk=f"projects/{PROJECT_ID}/zones/{ZONE}/disks/vm2",
    )
    op = images_client.insert(project=PROJECT_ID, image_resource=image)
    op.result()
    print("Image created")


def delete_vm2():
    print("Deleting vm2...")
    op = instances_client.delete(project=PROJECT_ID, zone=ZONE, instance="vm2")
    op.result()
    print("vm2 deleted (its disk is also deleted because auto_delete=True)")


def create_vm3():
    # vm3 has the same setup as vm2 but starts from the image we created
    print("Creating vm3 from the image...")

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
    print("vm3 is ready")


def cleanup():
    # remove the vm and image, but keep the network because later labs need it
    print("Deleting vm3...")
    op = instances_client.delete(project=PROJECT_ID, zone=ZONE, instance="vm3")
    op.result()

    print("Deleting the image...")
    op = images_client.delete(project=PROJECT_ID, image=IMAGE_NAME)
    op.result()

    print("Cleanup finished, ml-network has been kept")

def main():
    create_vm2()
    print("Waiting for the startup script to install nginx...")
    time.sleep(90)  # 90 seconds was enough in our tests, increase it if nginx is not ready in your case
    get_external_ip("vm2")
    input("Check that nginx works in the browser, then press enter...")

    create_image_from_vm2()
    delete_vm2()

    create_vm3()
    print("Waiting for vm3 to boot...")
    time.sleep(30)
    get_external_ip("vm3")
    input("Check that nginx also works on vm3, then press enter...")

    cleanup()

if __name__ == "__main__":
    main()
    
