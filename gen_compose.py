#!/usr/bin/env python3

import os
import sys
from copy import deepcopy
from pathlib import Path

import yaml
from dotenv import load_dotenv

load_dotenv()

MCP_SERVER_CONFIG = {
    "rag-server": {"env": "RAG_SERVER", "port": "8050"},
    "code-server": {"env": "CODE_SERVER", "port": "8051"},
    "web-search-server": {"env": "WEB_SEARCH_SERVER", "port": "8052"},
}
MCP_SERVICES = set(MCP_SERVER_CONFIG)


def expand_service(name, service, replicas):
    """Add replicated services"""
    services = {}

    for i in range(1, replicas + 1):
        s = deepcopy(service)
        replica_name = name if replicas == 1 else f"{name}-{i}"

        if "ports" in s:
            ports = s.pop("ports")
            s["expose"] = [
                p.split("}:")[1] if "}:" in p else p.split(":")[-1] for p in ports
            ]

        s["hostname"] = replica_name + "-${CLIMATECLAW_INSTANCE_NAME}"

        services[replica_name] = s

    return services


def expand_depends_on(depends_on, replica_counts):
    """Update the dependencies with names of replicated services"""
    if isinstance(depends_on, dict):
        expanded = {}
        for dependency, config in depends_on.items():
            replicas = replica_counts.get(dependency, 1)
            if replicas > 1:
                for i in range(1, replicas + 1):
                    expanded[f"{dependency}-{i}"] = deepcopy(config)
            else:
                expanded[dependency] = config
        return expanded

    if isinstance(depends_on, list):
        expanded = []
        for dependency in depends_on:
            replicas = replica_counts.get(dependency, 1)
            if replicas > 1:
                expanded.extend(f"{dependency}-{i}" for i in range(1, replicas + 1))
            else:
                expanded.append(dependency)
        return expanded

    return depends_on


def update_service_dependencies(services, replica_counts):
    """Update the service dependencies inhereted from the base compose"""
    for service in services.values():
        if "depends_on" in service:
            service["depends_on"] = expand_depends_on(
                service["depends_on"],
                replica_counts,
            )


def service_instance_names(name, replicas, services):
    if replicas == 1 and name in services:
        return [name]

    return [
        replica_name
        for replica_name in (f"{name}-{i}" for i in range(1, replicas + 1))
        if replica_name in services
    ]


def haproxy_dependencies(
    services,
    backend_n,
    litellm_n,
    available_mcp_servers,
    mcp_replica_n,
):
    dependencies = []
    dependencies.extend(service_instance_names("climateclaw", backend_n, services))

    for server in available_mcp_servers:
        dependencies.extend(
            service_instance_names(server, mcp_replica_n[server], services)
        )

    dependencies.extend(service_instance_names("litellm", litellm_n, services))
    dependencies.extend(["mongodb", "ollama"])

    return [dependency for dependency in dependencies if dependency in services]


def haproxy_backend(name, port, service_names, sticky_mode=None):
    lines = []
    lines.append(f"backend be_{name}")
    if sticky_mode:
        lines.append(f"    balance {sticky_mode}")
        lines.append("    hash-type consistent")

    for i, service_name in enumerate(service_names, start=1):
        lines.append(f"    server {name}{i} {service_name}:{port} check")

    lines.append("")
    return "\n".join(lines)


def generate_haproxy(
    services, backend_n, backend_port, litellm_n, server_list, replica_dict, port_dict
):
    conf = []

    conf.append(
        "global\n"
        "    daemon\n"
        "    maxconn 256\n"
        "\n"
        "defaults\n"
        "    mode http\n"
        "    timeout connect 5s\n"
        "    timeout client  60s\n"
        "    timeout server  60s\n"
        "    default-server inter 3s fall 3 rise 2\n"
        "\n"
    )

    conf.append(
        "frontend fe_backend\n"
        f"    bind *:{backend_port}\n"
        "    default_backend be_climateclaw\n"
        "\n"
    )

    conf.append(
        "frontend fe_litellm\n    bind *:4000\n    default_backend be_litellm\n\n"
    )

    for s in server_list:
        conf.append(
            f"frontend fe_{s}\n"
            f"    bind *:{port_dict[s]}\n"
            f"    default_backend be_{s}\n"
            "\n"
        )

    conf.append(
        haproxy_backend(
            "climateclaw",
            backend_port,
            service_instance_names("climateclaw", backend_n, services),
            "url_param thread_id",
        )
    )

    conf.append(
        haproxy_backend(
            "litellm",
            4000,
            service_instance_names("litellm", litellm_n, services),
        )
    )

    for s in server_list:
        conf.append(
            haproxy_backend(
                s,
                port_dict[s],
                service_instance_names(s, replica_dict[s], services),
                "hdr(thread-id)",
            )
        )

    return "\n".join(conf)


def generate_outer_haproxy(
    node_addresses: list[str],
    backend_port: str,
) -> str:
    """Generate an HAProxy config routing between node-local HAProxies."""

    if not node_addresses:
        raise ValueError("At least one node address must be configured")

    lines = [
        "global",
        "    daemon",
        "    maxconn 4096",
        "",
        "defaults",
        "    mode http",
        "    option httplog",
        "    option redispatch",
        "    timeout connect 5s",
        "    timeout client  600s",
        "    timeout server  600s",
        "    timeout tunnel  1h",
        "    default-server inter 3s fall 3 rise 2",
        "",
        "frontend fe_outer_climateclaw",
        f"    bind *:{backend_port}",
        "    default_backend be_node_haproxys",
        "",
        "backend be_node_haproxys",
        "    balance url_param thread_id check_post",
        "",
        "    option httpchk",
        "    http-check send meth GET uri /healthz",
        "    http-check expect status 200",
        "",
    ]

    for index, host in enumerate(node_addresses, start=1):
        lines.append(f"    server node{index} {host}:{backend_port} check")

    lines.append("")
    return "\n".join(lines)


def add_outer_haproxy_service(
    services: dict,
    network_name: str,
    backend_port: str,
    target_port: str,
    config_path: str,
) -> None:
    """Add the outer HAProxy to the generated Compose services."""

    services["outer-haproxy"] = {
        "image": "haproxy:3.0-alpine",
        "user": "0:0",
        "restart": "unless-stopped",
        "ports": [
            f"{target_port}:{backend_port}",
        ],
        "volumes": [
            (f"./{config_path}:/usr/local/etc/haproxy/haproxy.cfg:ro"),
        ],
        "networks": [network_name],
        "depends_on": {
            "haproxy": {
                "condition": "service_started",
            },
        },
    }


def main():

    if len(sys.argv) < 2:
        print("Usage: gen_compose.py docker-compose.dev.yml")
        sys.exit(1)

    compose_path = sys.argv[1]

    # Read env variables
    backend_port = os.environ.get("CLIMATECLAW_BACKEND_PORT", "8502")
    backend_target_port = os.environ.get("CLIMATECLAW_TARGET_PORT", "8502")
    backend_n = int(os.environ.get("CLIMATECLAW_BACKEND_REPLICAS", "1"))
    litellm_n = int(os.environ.get("CLIMATECLAW_LITELLM_REPLICAS", "1"))
    available_mcp_servers = [
        s
        for s in os.environ.get("CLIMATECLAW_AVAILABLE_MCP_SERVERS", "").split(",")
        if s.strip()
    ]
    mcp_replica_n = {
        s: int(
            os.environ.get(f"CLIMATECLAW_{MCP_SERVER_CONFIG[s]['env']}_REPLICAS", "1")
        )
        for s in available_mcp_servers
    }
    port_dict = {
        s: os.environ.get(
            f"CLIMATECLAW_{MCP_SERVER_CONFIG[s]['env']}_PORT",
            MCP_SERVER_CONFIG[s]["port"],
        )
        for s in available_mcp_servers
    }
    node_addresses = [
        address.strip()
        for address in os.environ.get(
            "CLIMATECLAW_NODE_ADDRESSES",
            "",
        ).split(",")
        if address.strip()
    ]

    # Check for different modes of deployment
    DEV_MODE = True if "dev" in compose_path else False
    CLUSTER_MODE = True if len(node_addresses) > 1 else False
    if DEV_MODE and CLUSTER_MODE:
        raise ValueError(
            "CLIMATECLAW_NODE_ADDRESSES is only supported "
            "for production compose generation"
        )

    outer_haproxy_config_path = "haproxy.outer.cfg"

    # Read base compose file
    base = yaml.safe_load(open(compose_path))

    services = base["services"]
    new_services = {}
    replica_counts = {
        "climateclaw": backend_n,
        "litellm": litellm_n,
        **mcp_replica_n,
    }

    # Replicate services
    for name, svc in services.items():
        if name == "climateclaw":
            new_services.update(expand_service(name, svc, backend_n))
        elif name == "litellm":
            new_services.update(expand_service(name, svc, litellm_n))
        elif name in MCP_SERVICES:
            if name in available_mcp_servers:
                new_services.update(expand_service(name, svc, mcp_replica_n[name]))
        elif name == "freva-web":
            env = [
                e
                for e in svc.get("environment", [])
                if not e.startswith("CHAT_BOT_URL=")
            ]
            env.append(f"CHAT_BOT_URL=http://haproxy:{backend_port}")
            svc["environment"] = env
            new_services[name] = svc
        else:
            new_services[name] = svc

    # Update service dependencies on replicated services
    update_service_dependencies(new_services, replica_counts)

    ## Add HAProxy service to compose
    # HAProxy exposed ports for DEV
    dev_ports = [
        f"{backend_target_port}:{backend_port}",
    ]
    if port_dict.get("code-server"):
        dev_ports.append(f"{port_dict['code-server']}:{port_dict['code-server']}")

    # HAProxy exposed ports for production. We don't expose MDP server ports in prod.
    if CLUSTER_MODE:
        prod_ports = [f"{backend_port}:{backend_port}"]
    else:
        prod_ports = [f"{backend_target_port}:{backend_port}"]

    network_name = list(base["networks"].keys())[0]

    new_services["haproxy"] = {
        "image": "haproxy:3.0-alpine",
        "user": "0:0",
        "ports": dev_ports if DEV_MODE else prod_ports,
        "volumes": ["./haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro"],
        "networks": [network_name],
        "depends_on": haproxy_dependencies(
            new_services,
            backend_n,
            litellm_n,
            available_mcp_servers,
            mcp_replica_n,
        ),
    }

    # Generate HAProxy config for node
    haproxy_cfg = generate_haproxy(
        new_services,
        backend_n,
        backend_port,
        litellm_n,
        available_mcp_servers,
        mcp_replica_n,
        port_dict,
    )

    outer_haproxy_cfg = None

    if CLUSTER_MODE:
        outer_haproxy_cfg = generate_outer_haproxy(
            node_addresses=node_addresses,
            backend_port=backend_port,
        )

        add_outer_haproxy_service(
            services=new_services,
            network_name=network_name,
            backend_port=backend_port,
            target_port=backend_target_port,
            config_path=outer_haproxy_config_path,
        )

    out = base | {"services": new_services}

    input_path = Path(compose_path)

    output_path = input_path.with_name(f"{input_path.stem}.scaled{input_path.suffix}")

    output_path.write_text(yaml.dump(out, sort_keys=False))

    Path("haproxy.cfg").write_text(haproxy_cfg)
    generated_files = [
        output_path.name,
        "haproxy.cfg",
    ]

    if outer_haproxy_cfg is not None:
        Path(outer_haproxy_config_path).write_text(outer_haproxy_cfg)
        generated_files.append(outer_haproxy_config_path)

    print(f"Generated {', '.join(generated_files)}")


if __name__ == "__main__":
    main()
