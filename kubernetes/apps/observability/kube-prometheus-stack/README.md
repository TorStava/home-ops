# kube-prometheus-stack

## NAS Deployments

Runs on TrueNAS as the custom app **exporters** (Apps > Discover Apps > Custom App,
or `midclt call app.create` with `custom_compose_config_string`). Prometheus scrapes it
via `kubernetes/apps/external-services/truenas/app/scrapeconfig.yaml`; alerts are in
`prometheusrule.yaml` next to it.

```yaml
services:
  node-exporter:
    image: quay.io/prometheus/node-exporter:v1.12.1
    command:
      - --path.rootfs=/host/root
      - --path.procfs=/host/proc
      - --path.sysfs=/host/sys
      - --path.udev.data=/host/root/run/udev/data
      - --web.listen-address=0.0.0.0:9100
      - --collector.filesystem.mount-points-exclude=^/(sys|proc|dev|host|etc|var/lib/docker|mnt/\.ix-apps)($$|/)
      - --collector.vmstat.fields=^(oom_kill|pgpg|pswp|pg.*fault).*
    network_mode: host
    pid: host
    restart: unless-stopped
    volumes:
      - /:/host/root:ro,rslave
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
  smartctl-exporter:
    image: quay.io/prometheuscommunity/smartctl-exporter:v0.14.0
    command:
      - --web.listen-address=:9633
      - --smartctl.interval=300s
    ports:
      - "9633:9633"
    privileged: true
    user: root
    restart: unless-stopped
```
