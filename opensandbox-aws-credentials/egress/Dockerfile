# The OpenSandbox egress image plus the SigV4 re-signing addon (part 5).
#
# The addon cannot be set per sandbox: OPENSANDBOX_EGRESS_MITMPROXY_SCRIPT is not an allowed
# create-request env var on 1.2.0. A derived image that bakes it in, with the server's
# `[egress] image` pointing at that image, is the supported route. mitmproxy loads system.py
# (the Credential Vault) first and this addon after it.
#
# BASE is the release image mirrored into your ECR by ../../opensandbox-eks/03_images.sh, pinned
# by digest (../build_egress.sh looks the digest of :release-1.2.0 up and builds with it):
#   docker buildx build --platform linux/amd64 \
#     --build-arg BASE="$ECR_REGISTRY/opensandbox-eks/egress@sha256:<digest of release-1.2.0>" \
#     -t "$ECR_REGISTRY/opensandbox-eks/egress-sigv4:release-1.2.0" egress/
# The build context is sigv4_resign.py alone (egress/.dockerignore).
ARG BASE
FROM ${BASE}

COPY sigv4_resign.py /var/egress/custom/sigv4_resign.py

# Fail the build, not the sidecar: load the addon with the image's own python3 and mitmproxy,
# and require all four hooks (a missing `response` would silently drop the error-body redaction).
RUN PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -c "import importlib.util as u; \
s = u.spec_from_file_location('sigv4_resign', '/var/egress/custom/sigv4_resign.py'); \
m = u.module_from_spec(s); s.loader.exec_module(m); \
assert all(callable(getattr(m, hook, None)) for hook in ('requestheaders', 'request', 'responseheaders', 'response'))"

ENV OPENSANDBOX_EGRESS_MITMPROXY_SCRIPT=/var/egress/custom/sigv4_resign.py
