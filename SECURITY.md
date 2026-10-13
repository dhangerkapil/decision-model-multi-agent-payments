# Security

Please don't report security vulnerabilities through public GitHub issues. Use
[GitHub private vulnerability reporting](https://docs.github.com/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
for this repository instead.

Security design notes for deployments of this sample:

- The Foundry resource has local (key) authentication disabled. The agent, the console, and the tools use Microsoft Entra
  ID with managed identities or your sign-in, and role assignments are scoped to the resources they need.
- The console is a demo UI with a public endpoint and no user authentication. Before you use real data, add
  authentication (for example, Container Apps built-in authentication with Microsoft Entra ID), restrict ingress, and
  review the [hosted agent permissions](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions).
- The sample uses synthetic data only. Don't load personal or payment card data into it without the controls your
  compliance program requires.
