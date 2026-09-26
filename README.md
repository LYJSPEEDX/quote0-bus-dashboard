# Sydney 526 Quote/0 board

Serverless Python service that fetches TfNSW departures and sends them to a
MindReset Quote/0 e-ink display. Each configured location is one screen: a
header with the route and location name, and one row per direction. The default
location shows route 526 at Sydney Olympic Park, towards Strathfield from
Australia Ave opp Figtree Dr (`212726`) on the top row and towards Rhodes from
Australia Ave before Herb Elliott Ave (`212727`) on the bottom row.

A step-by-step operations handbook (in Chinese) covering local debugging,
Terraform, AWS deployment, day-to-day changes and troubleshooting is in
[`docs/HANDBOOK.md`](docs/HANDBOOK.md).

## Prerequisites

- Terraform 1.6+, Python 3 with pip, `zip`, and AWS CLI credentials with permission to
  create the Lambda, IAM roles/policies, EventBridge Scheduler schedule, and
  CloudWatch log group defined in `terraform/`.
- A TfNSW Open Data API token.
- A paired and network-connected Quote/0, its Dot App API key, device serial,
  and an Image API content task added in Dot App Content Studio (one per
  location when several are configured).

The service calls TfNSW's `departure_mon` endpoint and Quote/0's v2 Image API.
Quote/0 is battery-aware: a cloud push is applied when the sleeping device next
wakes, not necessarily immediately.

## Configure and deploy

```sh
cp terraform/terraform.tfvars.example terraform/secrets.auto.tfvars
# Edit secrets.auto.tfvars locally. It is ignored by Git.
chmod 600 terraform/secrets.auto.tfvars
# State lives in S3; create the bucket once (docs/HANDBOOK.md section 5.3), then:
cp terraform/backend.hcl.example terraform/backend.hcl   # fill in the bucket name

./scripts/package_lambda.sh
cd terraform
terraform init -backend-config=backend.hcl
terraform plan -out=tfplan
terraform apply tfplan
```

Terraform state is stored in a private, versioned, encrypted S3 bucket with
S3-native locking. The state includes Lambda environment variables, including
both API keys, so never make the bucket public or copy the state elsewhere.

## Configuration

Defaults are in `terraform/variables.tf`: the Olympic Park 526 location, 10:00 to
17:00 Sydney time, refreshing every 30 minutes. `peak_windows` (e.g.
`"16:30-18:30"`) optionally adds faster refreshes. `NORMAL_REFRESH_MINUTES` and
`PEAK_REFRESH_MINUTES` must be multiples of two because the scheduler ticks
every two minutes.

Quote/0 only displays a push when it next wakes (its own refresh interval, set in
Dot App). With the default `refresh_mode = "follow_device"`, each scheduler tick
reads the device's predicted next wake from its status API and pushes only in
the two minutes before it, so the screen shows data at most about two minutes
old; if the next wake is unknown it falls back to the fixed cadence above
(`refresh_mode = "fixed"` uses that cadence only). Each row shows the
next bus as a large countdown (counted from the "Updated HH:MM" header time)
with its clock time, and the two following buses as clock times, which stay
correct however late the image appears.

### Locations

`locations` (the `LOCATIONS` JSON environment variable in Lambda) lists the
screens. Each location is one route at one or two stops, usually the pair of
stops facing each other across the road, one per direction:

```hcl
locations = [
  {
    name     = "Olympic Park"
    route    = "526"
    task_key = "olympic-park"
    stops = [
      { id = "212726", label = "Strathfield" },
      { id = "212727", label = "Rhodes" },
    ]
  },
]
```

Each location is pushed to its own Quote/0 Image API task, and the device loop
cycles through them. With a single location `task_key` is optional; with
several, add one Image API task per location in Dot App and give each location
that task's unique key. Stop IDs come from TfNSW's `stop_finder` API or the
stop pole.

The Lambda does not call TfNSW or Quote/0 outside its configured active window.
If a location's upstream request fails, that location keeps its last valid
image, the other locations still refresh, and the invocation throws for
CloudWatch visibility.

## Test locally

```sh
python3 -m unittest discover -s tests -v
```

For a live run, copy `.env.example` to `.env` (git-ignored, `chmod 600`) and
fill in the keys, then:

```sh
python3 scripts/local_refresh.py          # fetch TfNSW and write board.png
python3 scripts/local_refresh.py --push   # also push the image to Quote/0
```

## Operational checks

```sh
aws logs tail /aws/lambda/quote0-busboard --follow --region ap-southeast-2
aws scheduler get-schedule --name quote0-busboard-refresh --region ap-southeast-2
```

## First live test

Once Terraform has applied successfully, use a manual Lambda invocation to
fetch current TfNSW data and push a screen update immediately. This is the only
path that bypasses the configured 10:00–17:00 refresh gate:

```sh
aws lambda invoke \
  --function-name quote0-busboard \
  --region ap-southeast-2 \
  --cli-binary-format raw-in-base64-out \
  --payload '{"force_refresh":true}' \
  first-test-response.json
cat first-test-response.json
```

The expected response contains `"status":"updated"`. If it fails, inspect
the CloudWatch log command above; do not add or print either API key while
troubleshooting.

No API Gateway, database, VPC, S3 bucket, or public inbound endpoint is
created.

The board uses DejaVu Sans from `src/fonts/` (Bitstream Vera license, see
`src/fonts/LICENSE-DejaVu.txt`) because the Lambda runtime has no system fonts.
