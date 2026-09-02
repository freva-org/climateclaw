#!/usr/bin/env python

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

DEV_MODE = os.environ.get("CLIMATECLAW_DEV", "0")


def expand_service(name, service, replicas):
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


def expand_ollama_service(name, service, replicas):
    services = expand_service(name, service, replicas)

    if replicas == 1:
        return services

    if not DEV_MODE:
        for i in range(1, replicas + 1):
            replica_name = f"{name}-{i}"
            services[replica_name]["devices"] = [f"nvidia.com/gpu={i - 1}"]

    return services


def expand_depends_on(depends_on, replica_counts):
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
    ollama_n,
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
    dependencies.append("mongodb")
    dependencies.extend(service_instance_names("ollama", ollama_n, services))

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
    services,
    backend_n,
    backend_port,
    litellm_n,
    ollama_n,
    server_list,
    replica_dict,
    port_dict,
    timeout,
):
    conf = []
    request_id_rules = (
        "    http-request set-header X-Request-Id %[req.hdr(X-Request-Id),default(%[uuid()])]\n"
        "    declare capture request len 64\n"
        "    http-request capture req.hdr(X-Request-Id) id 0\n"
    )

    conf.append(
        "global\n"
        "    daemon\n"
        "    maxconn 1024\n"
        "    log stdout format raw local0 info\n"
        f"    log {os.environ.get('CLIMATECLAW_SYSLOG_TARGET', 'stdout')} local0 info\n"
        "    stats socket /var/run/haproxy.sock mode 660 level admin\n"
    )

    conf.append(
        "defaults\n"
        "    mode http\n"
        "    timeout connect 5s\n"
        f"    timeout client {timeout}s\n"
        f"    timeout server {timeout}s\n"
        "    default-server inter 3s fall 3 rise 2\n"
        "    log     global\n"
        "    option  httplog\n"
        "    log-format '%t %ci:%cp %ft %b/%s Tq=%Tq Tw=%Tw Tc=%Tc Tr=%Tr Tt=%Tt "
        "status=%ST bytes=%B term=%ts conn=%ac/%fc/%bc/%sc/%rc "
        "request_id=%[capture.req.hdr(0)] %{+Q}r'\n"
    )

    conf.append(
        "frontend fe_backend\n"
        f"    bind *:{backend_port}\n"
        f"{request_id_rules}"
        "    default_backend be_climateclaw\n"
    )

    conf.append(
        "frontend fe_litellm\n"
        "    bind *:4000\n"
        f"{request_id_rules}"
        "    default_backend be_litellm\n"
    )

    conf.append(
        "frontend fe_ollama\n"
        "    bind *:11434\n"
        f"{request_id_rules}"
        "    default_backend be_ollama\n"
    )

    for s in server_list:
        conf.append(
            f"frontend fe_{s}\n"
            f"    bind *:{port_dict[s]}\n"
            f"{request_id_rules}"
            f"    default_backend be_{s}\n"
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

    conf.append(
        haproxy_backend(
            "ollama",
            11434,
            service_instance_names("ollama", ollama_n, services),
            "leastconn",
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


def main():

    if len(sys.argv) < 2:
        print("Usage: gen_compose.py docker-compose.dev.yml")
        sys.exit(1)

    compose_path = sys.argv[1]

    backend_port = os.environ.get("CLIMATECLAW_BACKEND_PORT", "8502")
    backend_target_port = os.environ.get("CLIMATECLAW_TARGET_PORT", "8502")
    backend_n = int(os.environ.get("CLIMATECLAW_BACKEND_REPLICAS", "1"))
    litellm_n = int(os.environ.get("CLIMATECLAW_LITELLM_REPLICAS", "1"))
    ollama_n = int(os.environ.get("CLIMATECLAW_OLLAMA_REPLICAS", "1"))

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
    mcp_request_timeout = int(os.getenv("CLIMATECLAW_MCP_REQUEST_TIMEOUT_SEC", "600"))

    base = yaml.safe_load(open(compose_path))

    services = base["services"]
    new_services = {}
    replica_counts = {
        "climateclaw": backend_n,
        "litellm": litellm_n,
        "ollama": ollama_n,
        **mcp_replica_n,
    }

    for name, svc in services.items():
        if name == "climateclaw":
            new_services.update(expand_service(name, svc, backend_n))
        elif name == "litellm":
            new_services.update(expand_service(name, svc, litellm_n))
        elif name == "ollama":
            new_services.update(expand_ollama_service(name, svc, ollama_n))
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

    update_service_dependencies(new_services, replica_counts)

    dev_ports = [
        f"{backend_target_port}:{backend_port}",
    ]
    if port_dict.get("code-server"):
        dev_ports.append(f"{port_dict['code-server']}:{port_dict['code-server']}")
    prod_ports = [
        f"{backend_target_port}:{backend_port}",
    ]

    network_name = list(base["networks"].keys())[0]

    haproxy_network = (
        {
            network_name: {
                "aliases": ["ollama"],
            }
        }
        if ollama_n > 1
        else [network_name]
    )

    log_dir = (
        "./logs/"
        if "dev" in compose_path
        else "/container/da/climateclaw-links/${CLIMATECLAW_INSTANCE_NAME}/logs"
    )

    new_services["haproxy"] = {
        "image": "haproxy:3.0-alpine",
        "user": "0:0",
        "ports": dev_ports if "dev" in compose_path else prod_ports,
        "volumes": [
            "./haproxy.cfg:/usr/local/etc/haproxy/haproxy.cfg:ro",
            f"{log_dir}:/app/logs",
        ],
        "command": [
            "sh",
            "-c",
            "haproxy -W -db -f /usr/local/etc/haproxy/haproxy.cfg "
            ">> /app/logs/haproxy.log 2>&1",
        ],
        "networks": haproxy_network,
        "depends_on": haproxy_dependencies(
            services=new_services,
            backend_n=backend_n,
            litellm_n=litellm_n,
            ollama_n=ollama_n,
            available_mcp_servers=available_mcp_servers,
            mcp_replica_n=mcp_replica_n,
        ),
    }

    out = base | {"services": new_services}

    input_path = Path(compose_path)

    output_path = input_path.with_name(f"{input_path.stem}.scaled{input_path.suffix}")

    output_path.write_text(yaml.dump(out, sort_keys=False))

    haproxy_cfg = generate_haproxy(
        services=new_services,
        backend_n=backend_n,
        backend_port=backend_port,
        litellm_n=litellm_n,
        ollama_n=ollama_n,
        server_list=available_mcp_servers,
        replica_dict=mcp_replica_n,
        port_dict=port_dict,
        timeout=mcp_request_timeout,
    )

    Path("haproxy.cfg").write_text(haproxy_cfg)

    print(f"Generated {output_path.name} and haproxy.cfg")


if __name__ == "__main__":
    main()
