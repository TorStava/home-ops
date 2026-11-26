#!/usr/bin/env python3
"""Pre-merge check for the Authentik app blueprints.

Runs without a cluster or a running Authentik. For every
resources/blueprints/apps/<slug>.yaml it checks that:

- the first entry meta-applies access-groups.yaml;
- there is exactly one provider and one application, and the
  application slug equals the file name;
- there is exactly one policy binding, and it is to an access group;
- OAuth2 providers set signing_key and list grant_types;
- every !Env AK_* name exists in the authentik-app-credentials template;
- the file is listed in the configMapGenerator as app-<slug>.yaml.

It also checks that outposts.yaml (once it exists) lists every proxy
provider found in the app files.

Exit status 1 with one line per problem, 0 when everything passes.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

APP_DIR = Path("kubernetes/apps/security/authentik/app")
BLUEPRINTS = APP_DIR / "resources/blueprints"
CREDENTIALS = APP_DIR / "externalsecret-app-credentials.yaml"
KUSTOMIZATION = APP_DIR / "kustomization.yaml"

ACCESS_GROUPS = {"Guests", "Household", "Operators"}
ACCESS_GROUPS_PATH = "mounted/cm-authentik-blueprints/access-groups.yaml"
PROVIDER_MODELS = {
    "authentik_providers_oauth2.oauth2provider",
    "authentik_providers_proxy.proxyprovider",
}


class Tag:
    """A custom Authentik tag (!Env, !Find, !KeyOf, ...), kept for inspection."""

    def __init__(self, name: str, value):
        self.name = name
        self.value = value

    def __repr__(self) -> str:
        return f"{self.name} {self.value!r}"


class Loader(yaml.SafeLoader):
    pass


def _tag(loader: Loader, suffix: str, node: yaml.Node) -> Tag:
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return Tag(f"!{suffix}", value)


Loader.add_multi_constructor("!", _tag)


def load(path: Path):
    with path.open() as f:
        return yaml.load(f, Loader=Loader)


def walk(value):
    yield value
    if isinstance(value, dict):
        for v in value.values():
            yield from walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from walk(v)
    elif isinstance(value, Tag):
        yield from walk(value.value)


def env_names(value) -> set[str]:
    names = set()
    for v in walk(value):
        if isinstance(v, Tag) and v.name == "!Env":
            names.add(v.value if isinstance(v.value, str) else v.value[0])
    return names


def find_name(value) -> str | None:
    """The name looked up by !Find [authentik_core.group, [name, X]]."""
    if isinstance(value, Tag) and value.name == "!Find":
        model, *query = value.value
        if model == "authentik_core.group" and query and query[0][0] == "name":
            return query[0][1]
    return None


def credential_keys() -> set[str]:
    for doc in yaml.safe_load_all(CREDENTIALS.read_text()):
        if doc and doc.get("kind") == "ExternalSecret":
            return set(doc["spec"]["target"]["template"]["data"])
    return set()


def generator_files() -> dict[str, str]:
    kust = yaml.safe_load(KUSTOMIZATION.read_text())
    files = {}
    for gen in kust.get("configMapGenerator", []):
        if gen["name"] != "authentik-blueprints":
            continue
        for item in gen.get("files", []):
            key, _, src = item.partition("=")
            files[key] = str(Path(src).as_posix()).removeprefix("./")
    return files


def check_app(path: Path, creds: set[str], generator: dict[str, str]) -> tuple[list[str], list[str]]:
    slug = path.stem
    rel = path.relative_to(APP_DIR).as_posix()
    problems: list[str] = []
    proxies: list[str] = []
    entries = (load(path) or {}).get("entries") or []

    def bad(msg: str) -> None:
        problems.append(f"{rel}: {msg}")

    first = entries[0] if entries else {}
    if not (
        first.get("model") == "authentik_blueprints.metaapplyblueprint"
        and (first.get("attrs") or {}).get("identifiers", {}).get("path") == ACCESS_GROUPS_PATH
    ):
        bad(f"first entry must be a metaapplyblueprint of {ACCESS_GROUPS_PATH}")

    providers = [e for e in entries if e.get("model") in PROVIDER_MODELS]
    apps = [e for e in entries if e.get("model") == "authentik_core.application"]
    bindings = [e for e in entries if e.get("model") == "authentik_policies.policybinding"]

    if len(providers) != 1:
        bad(f"expected exactly one provider, found {len(providers)}")
    if len(apps) != 1:
        bad(f"expected exactly one application, found {len(apps)}")
    elif (apps[0].get("identifiers") or {}).get("slug") != slug:
        bad(f"application slug must be '{slug}' (the file name)")

    if len(bindings) != 1:
        bad(f"expected exactly one policy binding, found {len(bindings)}")
    else:
        ids = bindings[0].get("identifiers") or {}
        group = find_name(ids.get("group"))
        if group not in ACCESS_GROUPS:
            bad(f"binding must be to one of {sorted(ACCESS_GROUPS)}, found {group!r}")
        if not isinstance(ids.get("target"), Tag):
            bad("binding target must be a !KeyOf the application")

    for p in providers:
        attrs = p.get("attrs") or {}
        if p["model"] == "authentik_providers_oauth2.oauth2provider" and not attrs.get("signing_key"):
            bad("OAuth2 provider must set signing_key (else tokens fall back to HS256)")
        if p["model"] == "authentik_providers_oauth2.oauth2provider" and not attrs.get("grant_types"):
            bad("OAuth2 provider must list grant_types (a provider created from a blueprint allows none)")
        if p["model"] == "authentik_providers_proxy.proxyprovider":
            proxies.append((p.get("identifiers") or {}).get("name"))

    for name in sorted(env_names(entries)):
        if name.startswith("AK_") and name not in creds:
            bad(f"!Env {name} is not in the authentik-app-credentials template")

    key = f"app-{slug}.yaml"
    if generator.get(key) != f"resources/blueprints/apps/{slug}.yaml":
        bad(f"not listed in configMapGenerator as {key}=./resources/blueprints/apps/{slug}.yaml")

    return problems, proxies


def check_outposts(proxies: list[str]) -> list[str]:
    path = BLUEPRINTS / "outposts.yaml"
    if not proxies:
        return []
    if not path.exists():
        return [f"{path.relative_to(APP_DIR)}: missing, but app files define proxy providers {proxies}"]
    listed = set()
    for v in walk(load(path)):
        if isinstance(v, Tag) and v.name == "!Find" and v.value[0] == "authentik_providers_proxy.proxyprovider":
            listed.add(v.value[1][1])
    missing = sorted(set(proxies) - listed)
    return [f"outposts.yaml: proxy providers not listed: {missing}"] if missing else []


def main() -> int:
    creds = credential_keys()
    generator = generator_files()
    problems: list[str] = []
    proxies: list[str] = []
    apps = sorted((BLUEPRINTS / "apps").glob("*.yaml"))
    for path in apps:
        p, x = check_app(path, creds, generator)
        problems += p
        proxies += x
    problems += check_outposts(proxies)

    for key, src in generator.items():
        if key.startswith("app-") and not (APP_DIR / src).exists():
            problems.append(f"kustomization.yaml: {key} points at missing {src}")

    for line in problems:
        print(f"::error::{line}" if "GITHUB_ACTIONS" in os.environ else line)
    print(f"checked {len(apps)} app blueprint(s): {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
