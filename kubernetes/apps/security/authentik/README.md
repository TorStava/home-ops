# Authentik

Applications, their providers, the access groups and who may open what are defined in code, as
[blueprints](https://docs.goauthentik.io/customize/blueprints/) in `app/resources/blueprints/`.
Users and group memberships are managed in the Authentik UI.

## The UI rule

Change these **only in code**:

- applications and providers
- access groups (`Guests`, `Household`, `Operators`)
- policy bindings: which group may open which application
- outposts

Use the UI for:

- users and group memberships
- an emergency fix, which must then be written back to code. The nightly sync overwrites anything
  described in code.

## Access groups

Groups are nested: `Operators` is a child of `Household`, which is a child of `Guests`. Each
application is bound once, to the widest group that may open it, and members of that group's
children get in too: an app bound to `Guests` is open to all three groups. Users in no access
group are refused everywhere.

| Group | Who | Applications |
|---|---|---|
| Guests | Occasional or limited users | Immich |
| Household | Household adults | Paperless, Tandoor, Vikunja, Karakeep, Linkwarden, FreshRSS, Komga, Penpot, Outline, Actual Budget |
| Operators | Whoever runs the cluster | pgAdmin, Node-RED, Forgejo, gods-eye, Grafana |

Apps that map groups to in-app roles (Forgejo, Grafana) check only for `Operators`. The `groups`
claim lists direct memberships, not inherited parent groups.

## Adding an application

1. Put the client ID and secret in a Bitwarden item.
2. Copy an existing file in `app/resources/blueprints/apps/` to `<slug>.yaml`, where the file name
   equals the application slug. Keep the meta-apply of `access-groups.yaml` as the first entry and
   bind exactly one access group.
3. For a new OAuth2 provider, list `grant_types`. A provider created from a blueprint allows none
   by default.
4. Add the Bitwarden item to `app/externalsecret-app-credentials.yaml` as
   `AK_<SLUG>_CLIENT_ID`/`AK_<SLUG>_CLIENT_SECRET`, and add `app-<slug>.yaml` to the
   `authentik-blueprints` ConfigMap in `app/kustomization.yaml`.
5. For a proxy provider, add it to `app/resources/blueprints/outposts.yaml`.

`scripts/check-authentik-blueprints.py` checks these rules on every pull request.

After the merge, Authentik restarts and picks up the new file through blueprint discovery. That can
take up to an hour; to apply it right away, run the sync below.

## Nightly sync

The `authentik-blueprint-sync` CronJob runs at 03:30 Europe/Oslo with the same image and
credentials as Authentik (`app/resources/blueprint-sync.py`). It:

1. force-applies every blueprint, so UI edits to anything in code are undone and credentials
   rotated in Bitwarden reach Authentik within a day.
2. fails if an application has no `app-<slug>.yaml` or no access-group binding. The failed Job fires
   the `KubeJobFailed` alert, and the Job's log lists each problem as `PROBLEM ...`.

Run it now, for example after rotating a secret:

```sh
kubectl -n security create job --from=cronjob/authentik-blueprint-sync authentik-blueprint-sync-manual
kubectl -n security logs -f job/authentik-blueprint-sync-manual
kubectl -n security delete job authentik-blueprint-sync-manual
```

Rotating a client secret: update the Bitwarden item, wait for the `authentik-app-credentials` and
app ExternalSecrets to refresh (Reloader restarts Authentik), then run the sync.
