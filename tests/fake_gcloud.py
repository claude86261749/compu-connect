#!/usr/bin/env python3
"""A stand-in for gcloud, for tests/test_gcp_tunnel.py: the commands relay/gcp-tunnel uses, against a project kept in
the JSON file $FAKE_GCLOUD_STATE. Every call that changes something is appended to its "changes".

An instance "runs its startup script" as soon as its metadata is set: its guest attribute mcp/status becomes
"ready REVISION", or "failed REVISION: …" if $FAKE_GCLOUD_FAIL is set.
"""

import json
import os
import sys

STATE = os.environ["FAKE_GCLOUD_STATE"]
MUTATING = {"create", "update", "delete", "enable", "add-metadata", "remove-metadata", "start"}


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {"account": "someone@example.com", "project": "fake-project", "services": [], "networks": {},
                "firewalls": {}, "addresses": {}, "instances": {}, "dns": {}, "changes": [], "next_ip": 1}  # fmt: skip


def save(s):
    with open(STATE, "w") as f:
        json.dump(s, f, indent=1)


def not_found(what):
    sys.stderr.write("ERROR: (gcloud) The resource '%s' was not found\n" % what)
    sys.exit(1)


def out(obj):
    print(json.dumps(obj))


def split(argv):
    """(positional, {flag: value})"""
    pos, flags = [], {}
    for a in argv:
        if a.startswith("--"):
            k, _, v = a[2:].partition("=")
            flags[k] = v if _ else True
        else:
            pos.append(a)
    return pos, flags


def from_files(spec):
    md = {}
    for part in spec.split(","):
        k, path = part.split("=", 1)
        with open(path) as f:
            md[k] = f.read()
    return md


def boot(s, inst):
    """What the startup script reports once it has run with the instance's metadata."""
    md = {i["key"]: i["value"] for i in inst["metadata"]["items"]}
    rev = md.get("mcp-revision", "manual")
    if md.get("enable-guest-attributes") != "TRUE":
        return
    assert "startup-script" in md and "mcp-domain" in md and "BEGIN OPENSSH PRIVATE KEY" in md.get("mcp-host-key", "")
    inst["guest"] = "failed %s: installing packages" % rev if os.environ.get("FAKE_GCLOUD_FAIL") else "ready " + rev


def main():
    s = load()
    pos, flags = split(sys.argv[1:])
    if pos[:2] == ["auth", "list"]:
        print(s["account"])
        return
    if pos[:2] == ["config", "get-value"]:
        print(s["project"])
        return
    assert flags.get("project") == s["project"], "wrong project: %r" % flags.get("project")
    verb = pos[2] if pos[0] in ("compute", "dns") else pos[1]
    if verb in MUTATING:
        s["changes"].append(" ".join(pos))

    if pos[:2] == ["services", "list"]:
        api = flags["filter"].split("=", 1)[1]
        if api in s["services"]:
            print(api)
    elif pos[:2] == ["services", "enable"]:
        s["services"].append(pos[2])

    elif pos[:2] == ["compute", "networks"]:
        name = pos[3]
        if verb == "describe":
            return out(s["networks"][name]) if name in s["networks"] else not_found(name)
        if verb == "create":
            s["networks"][name] = {"name": name, "autoCreateSubnetworks": True}
        if verb == "delete":
            assert not any(f["network"].endswith("/" + name) for f in s["firewalls"].values()), "network in use"
            assert not s["instances"], "network in use"
            del s["networks"][name]

    elif pos[:2] == ["compute", "firewall-rules"]:
        name = pos[3]
        if verb == "describe":
            return out(s["firewalls"][name]) if name in s["firewalls"] else not_found(name)
        if verb in ("create", "update"):
            fw = s["firewalls"].setdefault(name, {"name": name})
            if verb == "create":
                assert flags["network"] in s["networks"], "no network " + flags["network"]
                fw["network"] = "https://www.googleapis.com/compute/v1/projects/p/global/networks/" + flags["network"]
            allowed = {}
            for r in flags["rules"].split(","):
                proto, port = r.split(":")
                allowed.setdefault(proto, []).append(port)
            fw["allowed"] = [{"IPProtocol": p, "ports": ports} for p, ports in allowed.items()]
            fw["sourceRanges"] = flags["source-ranges"].split(",")
            fw["targetTags"] = flags["target-tags"].split(",")
        if verb == "delete":
            del s["firewalls"][name]

    elif pos[:2] == ["compute", "addresses"]:
        name = pos[3]
        key = flags["region"] + "/" + name
        if verb == "describe":
            return out(s["addresses"][key]) if key in s["addresses"] else not_found(name)
        if verb == "create":
            s["addresses"][key] = {"name": name, "address": "127.0.0.%d" % (100 + s["next_ip"]), "status": "RESERVED"}
            s["next_ip"] += 1
        if verb == "delete":
            assert s["addresses"][key]["status"] != "IN_USE", "address in use"
            del s["addresses"][key]

    elif pos[:2] == ["compute", "instances"]:
        name = pos[3]
        key = flags["zone"] + "/" + name
        inst = s["instances"].get(key)
        if verb == "describe":
            return out({k: v for k, v in inst.items() if k != "guest"}) if inst else not_found(name)
        if verb == "get-guest-attributes":
            if not inst or not inst.get("guest"):
                return not_found("guestAttributes")
            return out([{"namespace": "mcp", "key": "status", "value": inst["guest"]}])
        if verb == "get-serial-port-output":
            print("Nov 21 10:00:00 relay google_metadata_script_runner[1]: startup-script: mcp-relay: %s" % inst.get("guest"))
            return
        if verb != "create" and inst is None:
            return not_found(name)
        if verb == "create":
            assert key not in s["instances"], "already exists"
            assert flags["network"] in s["networks"], "no network"
            assert flags.get("no-service-account") and flags.get("no-scopes"), "the relay must have no service account"
            addr = [a for a in s["addresses"].values() if a["address"] == flags["address"]]
            assert addr and addr[0]["status"] == "RESERVED", "address not reserved or in use"
            addr[0]["status"] = "IN_USE"
            md = from_files(flags["metadata-from-file"])
            inst = s["instances"][key] = {
                "name": name,
                "status": "RUNNING",
                "machineType": "https://www.googleapis.com/compute/v1/projects/p/zones/z/machineTypes/" + flags["machine-type"],
                "tags": {"items": flags["tags"].split(",")},
                "networkInterfaces": [{"accessConfigs": [{"natIP": flags["address"]}]}],
                "metadata": {"items": [{"key": k, "value": v} for k, v in sorted(md.items())]},
            }
            boot(s, inst)
        if verb == "add-metadata":
            md = {i["key"]: i["value"] for i in inst["metadata"]["items"]}
            md.update(from_files(flags["metadata-from-file"]))
            inst["metadata"]["items"] = [{"key": k, "value": v} for k, v in sorted(md.items())]
            boot(s, inst)
        if verb == "remove-metadata":
            gone = flags["keys"].split(",")
            inst["metadata"]["items"] = [i for i in inst["metadata"]["items"] if i["key"] not in gone]
        if verb == "start":
            inst["status"] = "RUNNING"
        if verb == "delete":
            ip = inst["networkInterfaces"][0]["accessConfigs"][0]["natIP"]
            for a in s["addresses"].values():
                if a["address"] == ip:
                    a["status"] = "RESERVED"
            del s["instances"][key]

    elif pos[:2] == ["dns", "record-sets"]:
        key = flags["zone"] + "/" + pos[3]
        if verb == "describe":
            return out(s["dns"][key]) if key in s["dns"] else not_found(pos[3])
        if verb in ("create", "update"):
            assert (key in s["dns"]) == (verb == "update")
            s["dns"][key] = {"name": pos[3], "type": flags["type"], "ttl": int(flags["ttl"]), "rrdatas": [flags["rrdatas"]]}
        if verb == "delete":
            del s["dns"][key]
    else:
        sys.exit("fake gcloud: no such command: %s" % " ".join(pos))
    save(s)


if __name__ == "__main__":
    main()
