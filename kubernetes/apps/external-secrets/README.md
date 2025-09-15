In order to bootstrap External Secrets for use with Bitwarden Secrets Manager you need to do the following.

Go to Bitwarden Secrets Manager and create a project and a machine account.

Get the machine account token and create the `secret.yaml` file given below:

```yaml
# secret.yaml
# yaml-language-server: $schema=https://kubernetesjsonschema.dev/v1.18.1-standalone-strict/secret-v1.json
apiVersion: v1
kind: Secret
metadata:
    name: bitwarden-secret
    namespace: external-secrets
    labels:
        reconcile.external-secrets.io/managed: "true"
stringData:
    token: "REPLACEME_bitwarden_machine_token"
```

Apply the secret to the cluster: `kubectl apply -f secret.yaml`

Get the OrganizationID and ProjectID and update the `store/clustersecretstore.yaml` file with your values.



