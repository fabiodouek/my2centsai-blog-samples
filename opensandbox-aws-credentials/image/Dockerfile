# The workload image for part 5: part 4's egress-tools image (python:3.12-slim plus curl, git, Node, the
# AWS CLI v2 2.39.3 and requests) with boto3 added, so the sandbox can call AWS with the CLI and with
# boto3. Built for linux/amd64 and pushed to ECR by ../00_image.sh. The base is in the cluster's own ECR
# (built by ../opensandbox-egress/00_image.sh from public.ecr.aws), so nothing is pulled through Docker Hub.
ARG BASE=set-by-00_image.sh
FROM ${BASE}

# The same pin as the broker's requirements. pip runs at build time on the laptop's network; the sandbox
# itself can reach only AWS.
RUN pip install --no-cache-dir boto3==1.43.123
