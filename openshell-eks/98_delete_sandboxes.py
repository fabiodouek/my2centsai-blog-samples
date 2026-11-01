"""Delete every sandbox on the osh-eks gateway (all pages), and report any workspace PVC left."""

from common import NAMESPACE, client, delete_all_sandboxes, kubectl_json, require_gateway


def main() -> None:
    require_gateway()
    with client() as c:
        print(f"deleted {delete_all_sandboxes(c)}")
    left = kubectl_json("-n", NAMESPACE, "get", "pvc", "-l", "openshell.ai/managed-by=openshell")["items"]
    print(f"workspace PVCs left: {len(left)}")


if __name__ == "__main__":
    main()
