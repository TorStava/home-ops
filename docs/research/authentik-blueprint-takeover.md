# Authentik blueprints: taking over UI-made objects

Research for issue #789 (map #787). It answers: what happens when a blueprint describes an object
that already exists because someone made it in the UI.

**Version.** `kubernetes/apps/security/authentik/app/ocirepository.yaml` pins chart tag `2026.8.5`,
which ships Authentik `2026.8.5`. Every source link below points at git tag `version/2026.8.5` of
[goauthentik/authentik](https://github.com/goauthentik/authentik/tree/version/2026.8.5) and tag
`authentik-2026.8.5` of [goauthentik/helm](https://github.com/goauthentik/helm/tree/authentik-2026.8.5).
The docs are the copies in that tag under `website/docs/customize/blueprints/`, which docs.goauthentik.io
publishes as `/customize/blueprints/`. Paths below are short: `importer.py` means
`authentik/blueprints/v1/importer.py` and `common.py` means `authentik/blueprints/v1/common.py`.

Researched Sun 22 Nov 2026 by reading the source. Nothing was run against the cluster.

## TL;DR: what this means for the plan

1. **An unchanged file is not reapplied on a schedule.** The hourly job applies a file only when its
   SHA-512 has changed. A later UI edit survives until the file changes, someone presses **Apply**, or
   the instance is re-created. So drift is not corrected by itself, and the next unrelated change to
   that file silently undoes the drift.
2. **Changing a secret does not reapply anything.** `!Env` and `!File` are read only during an apply.
   If the value in Bitwarden changes but the YAML does not, nothing is reapplied, and a pod restart
   does not help either.
3. **Removing an entry deletes nothing.** Removing a file deletes nothing either: the
   BlueprintInstance record goes, the objects stay. Only an explicit `state: absent` deletes, and
   Django cascades that delete.
4. **One bad entry fails the whole file.** The file is validated in a rolled-back dry run first and
   then applied in one transaction. A failed file is retried every hour until it passes.
5. **`!Find` that matches nothing quietly returns `null`.** It is an error only when the field cannot
   be null. A typo on an optional foreign key, such as `signing_key`, clears the field with no error.
6. **Identifiers that miss create a new object rather than failing.** For an OAuth2 provider, a new
   object means a fresh random client_id and secret. Setting `client_id` explicitly turns a miss into
   a unique-constraint failure. That failure stops the file, which is the safe outcome.

## 1. How identifiers match existing objects

Source: `Importer._validate_single` and `__query_from_identifier` in
[`importer.py`](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/blueprints/v1/importer.py)
(lines 198–328).

- All tags in `identifiers` are resolved first. The importer then builds this query:
  `Q(pk=<pk>) | (Q(k1=v1) & Q(k2=v2) …)`. Identifiers other than `pk` are **ANDed together**, and
  `pk`, when given, is **ORed** with that group. A dict value becomes a `<field>__contains` JSON
  lookup.
- `model.objects.filter(query).first()` picks the object. When more than one row matches, the first
  one wins and nothing warns about it. An identifier that is not a field fails the file with
  `Invalid identifier field`.
- **Match:** the serializer is built with `instance=<existing>, partial=True`. Only the keys you give
  are validated and written.
- **No match:** a new object is created from `attrs` merged with `identifiers`. Fields left out take
  model defaults; for an OAuth2 provider that means a new random `client_id` and `client_secret`.
  Required fields (see §5) must then be present.
- The importer merges identifiers into the data on **update** as well as on create
  (`always_merger.merge(full_data, updated_identifiers)`, line 304). For plain values this is
  harmless because they match the row already. For a dict identifier, the update writes the dict as
  the field's **whole** value.

**Docs vs code:**
- `structure.mdx` says "On update: only attrs are applied". The code also writes the identifiers.
- A code comment in `_validate_single` says identifiers are chained into an "OR query". The code
  ANDs every identifier other than `pk`. The docs (`structure.mdx`) describe the AND correctly.

**Identifiers to use for the takeover.** Each must be unique and must equal the live value exactly:

| Model | Identifier | Why |
|---|---|---|
| `authentik_providers_oauth2.oauth2provider` | `name` | `Provider.name` is `unique=True` ([core/models.py](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/core/models.py) L633) |
| `authentik_providers_proxy.proxyprovider` | `name` | same base class |
| `authentik_core.application` | `slug` | `slug` is unique; `name` is not |
| `authentik_core.group` | `name` | |
| `authentik_outposts.outpost` (embedded) | `managed: goauthentik.io/outposts/embedded` | [outposts/apps.py](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/outposts/apps.py) L25 |
| `authentik_policies.policybinding` | see §9 | |

Take the names from the live export in `~/data/home-ops/`, not from memory. A one-character
difference creates a second object.

## 2. States: `present`, `created`, `must_created`, `absent`

Source: `BlueprintEntryDesiredState` in
[`common.py`](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/blueprints/v1/common.py)
L58–64, and `Importer._apply_models` in `importer.py` L389–451.

- `present` (the default): create the object, or update the fields named in `attrs`. Other fields
  are left alone. This is the state for the takeover.
- `created`: create the object if it is missing; if it exists, leave it and skip the save. The entry
  is **still validated** against the existing object, so a `!Find` that resolves to `null` on a
  required field still fails the file. `!KeyOf` to the entry still works.
- `must_created`: fail the whole file if the object exists. It is useless for a takeover, because
  every object already exists.
- `absent`: delete with Django's `instance.delete()` if the object exists, and skip if it does not.
  The entry may fail validation and the delete still runs. The delete cascades: removing an
  application deletes its policy bindings (`PolicyBinding.target` is `CASCADE`), and removing a
  provider leaves the application with `provider = NULL` (`SET_DEFAULT`).

## 3. Does reapply overwrite later UI edits?

Sources:
- `blueprints_discovery`, `check_blueprint_v1_file` and `BlueprintEventHandler` in
  [`v1/tasks.py`](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/blueprints/v1/tasks.py)
  L76–187.
- The schedules in
  [`blueprints/apps.py`](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/blueprints/apps.py)
  L163–178.

What triggers an apply:

1. **Hourly discovery.** It runs at a random minute each hour (`fqdn_rand`) and also once at worker
   start (`send_on_startup=True`). It calls `apply_blueprint` **only if `instance.last_applied_hash`
   differs from the SHA-512 of the file bytes**.
2. **The file watcher** in the worker, using watchdog on `/blueprints`:
   - a created file starts a discovery, which again applies only when the hash changed;
   - a modified file starts an apply of every enabled instance with that path.
3. **The Apply button** in the UI, or `POST /api/v3/managed/blueprints/<id>/apply/`
   ([`blueprints/api.py`](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/blueprints/api.py)
   L211–216).

Nothing else applies a file. In particular, neither a restart nor a version upgrade reapplies an
unchanged file.

**So:**
- A UI edit to a field that `attrs` sets **survives until that file is next applied**.
- That next apply then overwrites the field silently, with no diff. The cause can be any edit to the
  same file, or a manual Apply.
- Fields not named in `attrs` are never touched.
- List and many-to-many fields are replaced whole, never merged. These include `property_mappings`,
  `redirect_uris`, the outpost's `providers`, and the `sources` of the identification stage (already
  noted in `social-login.yaml`). An item added in the UI disappears on the next apply.

**Docs vs code:** the docs agree. `index.mdx` says "Files that are new or modified will be discovered
and applied. The default interval is 60 minutes." But `structure.mdx` describes `present` as "This
will overwrite any manual changes", which suggests a continuous overwrite. The overwrite happens only
when the file is applied.

**For the plan:** drift detection (one of the map's "not yet specified" items) needs its own
mechanism, because blueprints never correct drift by themselves. Pressing **Apply** on each instance
forces the code back over the UI.

## 4. Does removing an entry delete anything?

No.
- `_apply_models` acts only on the entries that are present in the file. It keeps no record of what
  it created before; `BlueprintInstance.managed_models` exists but nothing writes to it.
- When a file disappears, `clear_failed_blueprints` runs hourly and at startup (`v1/tasks.py`
  L241–248, `apps.py` L173–177). It deletes the **BlueprintInstance** whose file can no longer be
  read, and leaves every object.
- Docs (`index.mdx`): "File-based blueprints are automatically removed when they become unavailable.
  However, none of the objects created by those blueprints are affected."

To delete on purpose, keep the entry with `state: absent` for at least one apply, then remove it.

## 5. OAuth2 provider: client_id, client_secret, `!Env` and `!File`

Sources:
- `OAuth2Provider` in
  [`providers/oauth2/models.py`](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/providers/oauth2/models.py)
  L219–405.
- `OAuth2ProviderSerializer` in
  [`providers/oauth2/api/providers.py`](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/providers/oauth2/api/providers.py).
- The `Env` and `File` tags in `common.py` L255–299.

Both fields can be written from a blueprint:
- `client_id` is a CharField with `unique=True` and `default=generate_id`.
- `client_secret` is a CharField with `blank=True` and `default=generate_client_secret` (128 chars).
- Both are in `Meta.fields`. `secret_fields` only hides the secret when the API *reads* the object.
- Values must be printable ASCII (`validate_client_id` and `validate_client_secret`).

**Keeping the current values:**
- **Leave both keys out of `attrs`.** A partial update leaves them as they are. This is the
  zero-risk first step of a takeover.
- **Weakness:** on an empty database, such as a lost database rebuilt from code, the objects come
  back with new random credentials and every app breaks. For code to be the full source of truth,
  copy the current values into Bitwarden, pass them through the ExternalSecret into
  `authentik-secret`, and set `client_id: !Env X` and `client_secret: !Env Y`. The repo is public, so
  even the client_id must not be written inline.

**How `!Env` behaves** (`Env.resolve` is `getenv(key) or default`):
- An unset or **empty** variable gives the default, which is `null` unless you give one. `client_id`
  and `client_secret` do not accept null, so the entry fails validation and the **whole file** is
  not applied. That fails safe: an empty secret never reaches the provider.
- `!Env` is resolved in the process that applies the blueprint, which is the **worker**. The chart
  passes `global.envFrom` to the worker
  ([helm `templates/worker/deployment.yaml`](https://github.com/goauthentik/helm/blob/authentik-2026.8.5/charts/authentik/templates/worker/deployment.yaml)
  L82–89), so `authentik-secret` reaches it.
- Any variable name works. The `AUTHENTIK_` prefix is not needed (see `GOOGLE_CLIENT_ID` in
  `social-login.yaml`).

**How `!File` behaves:** it reads the whole file and strips whitespace. On an `OSError` it logs a
warning and returns the default (`null`), which fails as above. The path is inside the worker
container. A Secret listed in the chart's `blueprints.secrets` is mounted at
`/blueprints/mounted/secret-<name>/`, but then authentik also treats every `*.yaml` key in that
Secret as a blueprint.

**Docs vs code:** `tags.mdx` says the default is given as a scalar, `!Env my_env_var, default`. The
code reads a default only from the **sequence** form, `!Env [my_env_var, default]`. The scalar form
looks up a variable literally named `my_env_var, default`.

**Rotation (changes the plan):**
- The file hash covers the YAML text only, not the resolved values. A new secret value in Bitwarden
  therefore does not trigger an apply.
- Reloader restarts the pod, but startup discovery sees the same hash and skips the file.
- After rotating a secret, press **Apply** on that instance, or change the file (for example, a
  comment that records the rotation date).

## 6. Other OAuth2 provider fields in this version

- **`redirect_uris`:** a list of objects, not a newline-separated string. The format is:
  ```yaml
  redirect_uris:
    - matching_mode: strict        # or: regex (checked with re.compile)
      url: https://app.example/callback
      redirect_uri_type: authorization   # optional; default authorization; or: logout
  ```
  It is stored as JSON in `_redirect_uris`, and the list is replaced whole on update.
  `redirect_uri_type: logout` entries are the post-logout redirects.
- **`signing_key`:** a nullable foreign key to `authentik_crypto.certificatekeypair`, matched with
  `!Find [authentik_crypto.certificatekeypair, [name, <keypair name>]]`. **If the `!Find` misses, the
  provider silently loses its key**:
  - `jwt_key` falls back to **HS256, signed with the client secret** (models.py L356–363);
  - clients that check RS256 against JWKS then fail.

  Check the key name against the export, or leave `signing_key` out to keep the current key.
  `encryption_key` behaves the same way (when set, tokens become JWEs).
- **`property_mappings`:** a many-to-many list of scope mapping pks, replaced whole. Refer to the
  built-in mappings by their stable `managed` value rather than by name, for example
  `!Find [authentik_providers_oauth2.scopemapping, [managed, goauthentik.io/providers/oauth2/scope-openid]]`.
  The values in this version, from
  [`blueprints/system/providers-oauth2.yaml`](https://github.com/goauthentik/authentik/blob/version/2026.8.5/blueprints/system/providers-oauth2.yaml),
  all start with `goauthentik.io/providers/oauth2/scope-`. The suffixes are `openid`, `email`,
  `profile`, `entitlements`, `offline_access`, `authentik_api`, `dcr` and `bound_key`. A `!Find`
  inside this list that misses gives `null`, which the related field rejects, so the file fails
  rather than dropping the mapping. That follows from DRF behaviour; we have not tested it.
- **`authorization_flow` and `invalidation_flow`:** required and non-null when a provider is
  *created* (`ProviderSerializer.Meta.extra_write_kwargs`). On an update (partial), leave them out or
  give them correctly. A `!Find` that misses gives null, so the file fails safely.
- **`grant_types`:** this version has the field (an ArrayField). Leave it out to keep the live value.
- **Proxy provider:** `ProxyProvider` subclasses `OAuth2Provider`
  ([`providers/proxy/models.py`](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/providers/proxy/models.py)
  L73). Its serializer exposes `client_id` but **not** `client_secret`, and `redirect_uris` is
  read-only. Every save runs `set_oauth_defaults()` (L162–182), which:
  - forces `signing_key = None`;
  - re-adds the openid, profile, email, entitlements and proxy scopes;
  - rebuilds the redirect URIs from `external_host`.

  So only the proxy fields need describing (`external_host`, `mode`, `internal_host`, and so on). In
  `proxy` mode, `internal_host` is required. To list the proxy provider on the embedded outpost, add
  an entry for `authentik_outposts.outpost` (identifier `managed: goauthentik.io/outposts/embedded`)
  with `providers: [...]`. That list is replaced whole, so it must name every provider the outpost
  serves.

**Safety net against a duplicate provider:**
- If the `name` identifier misses, the importer *creates* a provider. If `client_id` is set
  explicitly, that insert breaks the unique constraint. `_save_with_retry` (importer.py L330–364)
  re-validates once, hits the constraint again, and fails the file.
- The application side has the same guard: `Application.provider` is a OneToOne, so two applications
  cannot point at one provider.
- So setting `client_id` turns "wrong name, silent new provider" into "file fails, nothing changes".

## 7. Ordering and dependencies across files

- Inside one file, entries are processed **in order**. `entries` may be a list or a dict of named
  sections; dict sections are processed in key order (`Blueprint.iter_entries`). `!KeyOf` finds only
  entries **earlier in the same file**; otherwise it raises an error and the file fails
  (`KeyOf.resolve`, common.py L230–252).
- Across files there is **no guaranteed order**. Each file is its own BlueprintInstance and its own
  `apply_blueprint` task, and these may run at the same time. Docs (`index.mdx`): "blueprint
  discovery and evaluation is not guaranteed to follow any specific order".
- There are two ways to depend on another file:
  - **`!Find`** for an object defined in another file. If that file has not been applied yet, a
    required field gets null and this file fails. It is retried every hour (a failed apply does not
    store `last_applied_hash`), so the files converge within an hour or two, but at the first apply
    they come up in a random order.
  - **The meta model `authentik_blueprints.metaapplyblueprint`**
    ([`v1/meta/apply_blueprint.py`](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/blueprints/v1/meta/apply_blueprint.py)),
    with `attrs.identifiers` set to `{path: mounted/cm-authentik-blueprints/<key>.yaml}` or
    `{name: <metadata.name>}`. It applies the dependency inline, before the entries that follow.
    - `required: true` (the default) fails the file if no such BlueprintInstance exists **yet**.
      Discovery creates instances in the same loop, so the first discovery can hit this.
    - If the nested blueprint itself is invalid, its errors are only logged. The outer file goes on,
      and the nested instance's status is not updated.
- The ConfigMap is mounted at `/blueprints/mounted/cm-authentik-blueprints/`
  ([helm worker deployment](https://github.com/goauthentik/helm/blob/authentik-2026.8.5/charts/authentik/templates/worker/deployment.yaml)
  L102–104). Discovery skips any path component that starts with `.`, so the kubelet's `..data`
  directories are ignored. Only keys that end in `.yaml` are found.
- **Simplest for us:** keep objects that depend on each other in one file, so `!KeyOf` and the
  single transaction cover them. A good unit is one file per application: provider, application and
  bindings together. Shared objects (access groups, scope mappings) can live in a file that the
  others reach with `!Find`, accepting that convergence can take an hour on a fresh database.

## 8. `!Find` when nothing matches

`Find.resolve` (common.py L369–413) returns `model.objects.filter(<ANDed pairs>).first().pk`, or
**`None`** when nothing matches. `!FindObject` also returns `None`. The tag itself never raises,
except for an unknown model name. What happens next depends on the field:

- A field that cannot be null (`authorization_flow`, `invalidation_flow`, the binding `target`, an
  item of `property_mappings`) fails the entry, so the **whole file fails**. That is safe.
- A nullable field (`signing_key`, `encryption_key`, `authentication_flow`, `Application.provider`,
  `group` or `policy` on a binding) **is set to null without any error**. That is dangerous on a
  takeover.
- In `identifiers`, a `None` value becomes an `IS NULL` lookup. It usually matches nothing, so a new
  object is created.

**Docs vs code:** `tags.mdx` does not say what happens on no match. The code returns null, as above.
`!Find` takes any Django model (`apps.get_model`), including models a blueprint cannot write.

## 9. Policy bindings: matched, not duplicated

Sources:
- `PolicyBinding` in
  [`policies/models.py`](https://github.com/goauthentik/authentik/blob/version/2026.8.5/authentik/policies/models.py).
- `models.mdx` § `authentik_policies.policybinding`.

- Bindings have no name. The only database constraint is
  `unique_together = ("policy", "target", "order")`. In PostgreSQL, NULLs are distinct, so **group
  and user bindings (where `policy` is NULL) have no unique constraint at all**. A binding whose
  identifiers miss is created as a duplicate without complaint.
- Exactly one of `policy`, `group` or `user` must be set.
- `target` is the pbm_uuid of the PolicyBindingModel. For an application, `!Find [authentik_core.application, [slug, <slug>]]`
  returns the application's pk, which is the same UUID (the parent link `policybindingmodel_ptr`).
  `!KeyOf <app-entry-id>` is special-cased to return `pbm_uuid` when used in a policybinding entry
  (common.py L240–247).
- **Recommended identifiers:**
  - group binding: `identifiers: {target: …, group: !Find [authentik_core.group, [name, <access group>]]}`,
    with `order`, `negate`, `enabled` and `timeout` in `attrs`;
  - policy binding: `identifiers: {target: …, policy: …}`.

  This matches the UI-made binding **whatever its order is**, and the order is then set from code.
  The docs' example of `target` + `order` matches only when the order is exactly the one the UI
  chose; otherwise it creates a second binding.
- **Beware:** if the group's `!Find` misses (a typo, or the group is not created yet), `group: None`
  becomes part of the lookup. The importer then tries to create a binding with no policy, group or
  user, and validation rejects it, so the file fails safely. But if the identifiers are
  `{target, order}` and `group` is in `attrs`, a group `!Find` that misses also fails, through the
  exactly-one check.
- Bindings on the application that are **not** in the file stay. A UI-added binding that grants
  access remains in force until it is removed by hand or with an `absent` entry.

## 10. How errors surface

Source: `apply_blueprint` (v1/tasks.py L190–238) and `Importer.validate` / `Importer.apply`
(importer.py L377–470).

- **Whole-file failure, never partial.** `validate()` first runs every entry in a transaction that is
  always rolled back. On the first invalid entry it stops (`return False`). The status becomes
  `error` and nothing is written. Only when the dry run passes does `apply()` run, again in one
  atomic transaction. Docs (`index.mdx` § Blueprint execution) say the same: "all-or-nothing".
- The log names **only the first** failing entry, so fixes can come one at a time.
- YAML errors, an unknown model, a model not allowed in blueprints, an invalid identifier field,
  serializer errors, a `!KeyOf` with no target, and a `must_created` object that already exists all
  fail the same way.
- **Where to look:**
  - **Customization → Blueprints**: the instance status is successful or error;
  - **System Tasks**: the `apply_blueprint` task logs, with uid = slug of the blueprint name;
  - the worker pod's logs, which show `Entry invalid: …` warnings.
- **Retry:** a failed apply does not store `last_applied_hash`, so the next hourly discovery tries
  again. A broken file therefore retries every hour, and nothing that file manages changes until it
  passes.
- One file failing does not affect the others. They are separate instances.

## Suggested takeover order (from the findings)

1. Export the live objects to `~/data/home-ops/` and copy exact names, slugs, key names and the
   current `client_id`/`client_secret` values into Bitwarden.
2. First PR: entries that use `state: present`, the right identifiers, and **no** credential keys.
   Leave out `signing_key`, or check its `!Find`. Nothing about login changes, and the instance shows
   `successful`.
3. Second PR: add `client_id: !Env …` and `client_secret: !Env …`, with values equal to the live
   ones, so the apply changes no row. Then an empty database can be rebuilt from code.
4. After that, edit only in code. To pull drift back, press **Apply**, or edit the file.
