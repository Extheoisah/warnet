import os
import sys
import yaml
import subprocess
import logging
from typing import List, Optional, Dict, Union

import networkx as nx
from .prometheus import generate_prometheus_config
from .conf_parser import parse_bitcoin_conf, dump_bitcoin_conf
from .addr import generate_ip_addr, DEFAULT_SUBNET
from .services import DockerComposeService, DockerComposeServicesDict

logging.basicConfig(level=logging.INFO)
DOCKER_COMPOSE_FILE = "docker-compose.yml"
DEFAULT_CONF = "config/bitcoin.conf"
NETWORK = 'regtest'


def get_architecture():
    """
    Get the architecture of the machine.

    :return: The architecture of the machine or None if an error occurred
    """
    try:
        result = subprocess.run(['uname', '-m'], stdout=subprocess.PIPE)
        arch = result.stdout.decode('utf-8').strip()
        if arch == "arm64":
            arch = "aarch64"
        if arch is not None:
            logging.info(f"Detected architecture: {arch}")
        else:
            raise Exception("Failed to detect architecture.")
        return arch
    except Exception as e:
        logging.error(f"An error occurred: {e}")
        return None


def write_bitcoin_configs(graph):

    with open(DEFAULT_CONF, 'r') as file:
        default_bitcoin_conf_content = file.read()
    default_bitcoin_conf = parse_bitcoin_conf(default_bitcoin_conf_content)

    for node_id, node_data in graph.nodes(data=True):
        # Start with a copy of the default configuration for each node
        node_conf = default_bitcoin_conf.copy()

        node_options = node_data.get("bitcoin_config", "").split(",")
        for option in node_options:
            option = option.strip()
            if option:
                if "=" in option:
                    key, value = option.split("=")
                else:
                    key, value = option, "1"
                node_conf[NETWORK][key] = value

        node_config_file = dump_bitcoin_conf(node_conf)

        with open(f"config/bitcoin.conf.{node_id}", 'w') as file:
            file.write(node_config_file)


def generate_docker_compose(graph_file: str):
    """
    Generate a docker-compose.yml file for the given graph.

    :param version: A list of Bitcoin Core versions
    :param node_count: The number of nodes in the graph
    """
    arch = get_architecture()

    # Delete any previous existing file
    # Reason: If the graph file we are importing has any errors,
    # we want the whole process to fail. Otherwise we may silently
    # just run with whatever .yml we had created on the last run
    try:
        os.remove(DOCKER_COMPOSE_FILE)
    except:
        pass

    graph = nx.read_graphml(graph_file, node_type=int)
    nodes = [graph.nodes[node] for node in graph.nodes()]

    write_bitcoin_configs(graph)
    generate_prometheus_config(len(nodes))

    services = DockerComposeServicesDict()

    services.add_service(
        name="prometheus",
        container_name="prometheus",
        image="prom/prometheus:latest",
        ports=["9090:9090"],
        volumes=["./prometheus.yml:/etc/prometheus/prometheus.yml"],
        command=["--config.file=/etc/prometheus/prometheus.yml"],
        networks=["warnet"]
    )

    services.add_service(
        name="node-exporter",
        container_name="node-exporter",
        image="prom/node-exporter:latest",
        volumes=[
            "/proc:/host/proc:ro",
            "/sys:/host/sys:ro",
            "/:/rootfs:ro"
        ],
        command=["--path.procfs=/host/proc", "--path.sysfs=/host/sys"],
        networks=["warnet"]
    )

    services.add_service(
        name="grafana",
        container_name="grafana",
        image="grafana/grafana:latest",
        ports=["3000:3000"],
        volumes=["grafana-storage:/var/lib/grafana"],
        networks=["warnet"]
    )

    volumes = {
        "grafana-storage": None,
    }

    for i, node in enumerate(nodes):
        version = node["version"]
        conf_file = node.get("conf", "bitcoin.conf")
        conf_file_path = f"./config/{conf_file}"

        # Check if the configuration file exists
        if not os.path.isfile(conf_file_path):
            print(f"Error: Configuration file {conf_file_path} does not exist.")
            sys.exit(1)  # Exit with an error code

        if "/" and "#" in version:
            # it's a git branch, building step is necessary
            repo, branch = version.split("#")
            build = {
                "context": ".",
                "dockerfile": "Dockerfile_build",
                "args": {
                    "REPO": repo,
                    "BRANCH": branch,
                }
            }
        else:
            # assume it's a release version, get the binary
            build = {
                "context": ".",
                "dockerfile": "Dockerfile_release",
                "args": {
                    "ARCH": arch,
                    "BITCOIN_VERSION": version,
                    "BITCOIN_URL": f"https://bitcoincore.org/bin/bitcoin-core-{version}/bitcoin-{version}-{arch}-linux-gnu.tar.gz"
                }
            }

        # TODO: we may need unique service names to bust cache if .yml file changes
        ip_addr = generate_ip_addr(DEFAULT_SUBNET)
        logging.debug(f"Using ip addr {ip_addr} for node {i}")

        # Add bitcoin-node service
        services.add_service(
            name=f"bitcoin-node-{i}",
            container_name=f"warnet_{i}",
            build=build,
            volumes=[f"{conf_file_path}:/root/.bitcoin/bitcoin.conf"],
            networks={"warnet": {"ipv4_address": f"{ip_addr}"}}
        )

        # Add prom-exporter-node service
        services.add_service(
            name=f"prom-exporter-node-{i}",
            container_name=f"exporter-node-{i}",
            image="jvstein/bitcoin-prometheus-exporter",
            environment={
                "BITCOIN_RPC_HOST": f"bitcoin-node-{i}",
                "BITCOIN_RPC_PORT": 18443,
                "BITCOIN_RPC_USER": "btc",
                "BITCOIN_RPC_PASSWORD": "passwd",
            },
            ports=[f"{8335 + i}:9332"],
            networks=["warnet"]
        )

    compose_config = {
        "version": "3.8",
        "services": dict(services),
        "volumes": volumes,
        "networks": {
            "warnet": {
                "name": "warnet",
                "ipam": {
                    "config": [
                        {"subnet": DEFAULT_SUBNET},
                    ]
                }
            }
        }
    }

    try:
        with open(DOCKER_COMPOSE_FILE, "w") as file:
            yaml.dump(compose_config, file)
    except Exception as e:
        logging.error(f"An error occurred while writing to docker-compose.yml: {e}")


