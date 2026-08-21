# envforge-agent

An agent that takes a script it does not trust, has an LLM write a Dockerfile for it,
builds the image, runs it inside a hardened container, reads the failure, repairs the
Dockerfile, and loops until it works or hits a cap. Then it reports what the script
tried to do while it ran.

Scope today: Python and Bash scripts, one file each. The agent runs on your machine as a
plain CLI and talks to your local Docker; nothing mounts the Docker socket, and the
untrusted script only ever runs inside a container with no network, a memory and process
cap, a read-only filesystem, no capabilities, and a non-root user.

Work in progress. The build order is in the commit history.
