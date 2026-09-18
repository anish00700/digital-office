# One image for every employee. Nothing of the host is in it: no home
# directory, no dotfiles, no credentials. The wrapper mounts the workspace
# and passes only the variables the CLI needs.
FROM node:22-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        git ca-certificates curl jq ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code \
    && useradd -m -u 1000 -s /bin/bash agent
USER agent
WORKDIR /workspace
ENV HOME=/home/agent
ENTRYPOINT ["claude"]
