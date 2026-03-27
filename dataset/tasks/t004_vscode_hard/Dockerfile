FROM node:20-bookworm-slim
RUN apt-get update && apt-get install -y git curl python3 python3-pip && rm -rf /var/lib/apt/lists/*
RUN npm install -g @google/gemini-cli
WORKDIR /repo
ARG BASE_COMMIT
RUN git clone https://github.com/microsoft/vscode . && git checkout ${BASE_COMMIT}
RUN npm ci
COPY harness/agent_runner.sh /scripts/agent_runner.sh
RUN chmod +x /scripts/agent_runner.sh
