# OpenTelemetry Demo → ClickStack on AWS EKS

A standing instance of the OpenTelemetry demo application on an AWS EKS cluster,
shipping traces, logs, metrics and session replay to a managed ClickStack
service on ClickHouse Cloud. It is the cloud counterpart of the laptop workshop
in `../observability-workshop/`: same ClickHouse service, same collector wiring,
same Helm values overlay and feature-flag exercise, but reachable by anyone with
the AWS profile rather than tied to one Mac. The cluster's node group scales to
zero when idle so only the EKS control plane is billed between demos.

Run `./demo.sh` with no subcommand to list what it can do.
