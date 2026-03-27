FROM python:3.11-slim
RUN apt-get update && apt-get install -y git curl nodejs npm && rm -rf /var/lib/apt/lists/*
RUN npm install -g @google/gemini-cli
WORKDIR /repo
ARG BASE_COMMIT
RUN git clone https://github.com/tiangolo/fastapi . && git checkout ${BASE_COMMIT}
RUN pip install -e ".[all,dev,doc,test]"
COPY harness/agent_runner.sh /scripts/agent_runner.sh
RUN chmod +x /scripts/agent_runner.sh
