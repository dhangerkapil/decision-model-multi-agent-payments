"""Entry point for the Payments Trust Fabric hosted agent (Responses protocol, port 8088)."""

from dotenv import load_dotenv

load_dotenv(override=False)

from agent_framework.observability import enable_instrumentation  # noqa: E402
from agent_framework_foundry_hosting import ResponsesHostServer  # noqa: E402

from trust_fabric.workflow import build_agent  # noqa: E402


def main() -> None:
    enable_instrumentation()
    server = ResponsesHostServer(build_agent)
    server.run()


if __name__ == "__main__":
    main()
