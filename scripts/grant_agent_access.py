"""azd postdeploy hook: let the hosted agent call Microsoft-Decision-1.

Each hosted agent gets its own Microsoft Entra identity when it's deployed. Model calls through the project endpoint are
allowed by default, but this solution calls Microsoft-Decision-1 on the Foundry resource endpoint, so the agent identity
needs the Foundry User role on the Foundry resource. The hook is idempotent; role assignments can take a few minutes to
take effect, and until then the agent fails safe (cases go to human triage).

Usage (azd runs it after `azd deploy` and `azd up`):  python scripts/grant_agent_access.py [--service <name>]
"""

import argparse
import json
import shutil
import subprocess
import sys

FOUNDRY_USER_ROLE = "53ca6127-db72-4b80-b1b0-d745d6d5456d"  # Foundry User (formerly Azure AI User)


def run(*args: str) -> str:
    exe = shutil.which(args[0])
    if not exe:
        sys.exit(f"'{args[0]}' was not found on PATH.")
    result = subprocess.run([exe, *args[1:]], capture_output=True, text=True, encoding="utf-8")
    if result.returncode != 0:
        sys.exit(f"Command failed: {' '.join(args)}\n{result.stderr.strip()}")
    return result.stdout


def find(node, key: str):
    """Depth-first search for a key in nested JSON."""
    if isinstance(node, dict):
        if key in node:
            return node[key]
        node = list(node.values())
    if isinstance(node, list):
        for child in node:
            found = find(child, key)
            if found is not None:
                return found
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", default="decision-payments-agent", help="Agent service name in azure.yaml")
    args = ap.parse_args()

    values = json.loads(run("azd", "env", "get-values", "--output", "json"))
    scope = values.get("AZURE_AI_ACCOUNT_ID")
    if not scope:
        sys.exit("AZURE_AI_ACCOUNT_ID is not set; run `azd provision` first.")

    agent = json.loads(run("azd", "ai", "agent", "show", args.service, "--output", "json", "--no-prompt"))
    identity = find(agent, "instance_identity") or {}
    principal = identity.get("principal_id") if isinstance(identity, dict) else None
    if not principal:
        sys.exit(f"Could not find the agent identity (instance_identity.principal_id) for '{args.service}'.")

    # No parentheses in arguments: on Windows az is a .cmd script, and cmd.exe would misread them.
    existing = [line for line in run("az", "role", "assignment", "list", "--assignee", principal, "--role", FOUNDRY_USER_ROLE,
                                     "--scope", scope, "--query", "[].id", "--output", "tsv").splitlines() if line.strip()]
    if existing:
        print(f"Agent identity {principal} already has Foundry User on the Foundry resource.")
        return
    run("az", "role", "assignment", "create", "--assignee-object-id", principal, "--assignee-principal-type",
        "ServicePrincipal", "--role", FOUNDRY_USER_ROLE, "--scope", scope, "--output", "none")
    print(f"Granted Foundry User on the Foundry resource to agent identity {principal}. "
          "It can take a few minutes to take effect.")


if __name__ == "__main__":
    main()
