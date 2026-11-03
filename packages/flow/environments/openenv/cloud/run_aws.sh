#!/bin/zsh
# One short N-session smoke run on a single tagged spot instance, then teardown.
#   environments/openenv/cloud/run_aws.sh [N] [EPISODES] [BRANCH]
# Results come back through the serial console log; nothing else is created.
set -eu
export AWS_PROFILE=openadapt AWS_REGION=us-east-1
N=${1:-100}; EPISODES=${2:-2}; BRANCH=${3:-feat/openenv-benches}
TYPE=c6a.8xlarge; MAX_PRICE=1.00; CAP_MIN=45
account=$(aws sts get-caller-identity --query Account --output text)
[[ $account == 992382684924 ]] || { echo "wrong AWS account $account"; exit 1; }
HERE=${0:A:h}
ami=$(aws ssm get-parameter --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id --query Parameter.Value --output text)
data=$(sed -e "s|__CAP_MIN__|$CAP_MIN|" -e "s|__BRANCH__|$BRANCH|" -e "s|__N__|$N|" -e "s|__EPISODES__|$EPISODES|" $HERE/user-data.sh)
tags="ResourceType=instance,Tags=[{Key=Project,Value=openadapt},{Key=Purpose,Value=openenv-parallel-smoke},{Key=Owner,Value=openenv-benches},{Key=Expires,Value=$(date -u -v+1d +%F)}]"
voltags="ResourceType=volume,Tags=[{Key=Project,Value=openadapt},{Key=Purpose,Value=openenv-parallel-smoke}]"
iid=$(aws ec2 run-instances --image-id $ami --instance-type $TYPE --count 1 \
  --instance-market-options "MarketType=spot,SpotOptions={MaxPrice=$MAX_PRICE,SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}" \
  --instance-initiated-shutdown-behavior terminate \
  --block-device-mappings 'DeviceName=/dev/sda1,Ebs={VolumeSize=30,VolumeType=gp3,DeleteOnTermination=true}' \
  --metadata-options HttpTokens=required \
  --tag-specifications "$tags" "$voltags" --user-data "$data" \
  --query 'Instances[0].InstanceId' --output text)
started=$(date -u +%FT%TZ)
echo "launched $iid ($TYPE spot, max \$$MAX_PRICE/h, cap ${CAP_MIN} min) at $started"
cleanup() { aws ec2 terminate-instances --instance-ids $iid >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
out=""
for i in $(seq 1 90); do
  sleep 30
  out=$(aws ec2 get-console-output --instance-id $iid --latest --output text --query Output 2>/dev/null || true)
  state=$(aws ec2 describe-instances --instance-ids $iid --query 'Reservations[0].Instances[0].State.Name' --output text)
  [[ $out == *OPENENV-SMOKE-JSON-END* || $state == terminated || $state == shutting-down ]] && break
done
cleanup
ended=$(date -u +%FT%TZ)
price=$(aws ec2 describe-spot-price-history --instance-types $TYPE --product-descriptions Linux/UNIX --max-items 1 --query 'SpotPriceHistory[0].SpotPrice' --output text)
print -r -- "$out" | sed -n '/OPENENV-SMOKE-JSON-BEGIN/,/OPENENV-SMOKE-JSON-END/p' | sed '1d;$d' | grep '^{' | tail -1 > $HERE/last-run.json || true
echo "{\"instance\": \"$iid\", \"type\": \"$TYPE\", \"started\": \"$started\", \"ended\": \"$ended\", \"spot_price_per_hour\": $price, \"n\": $N, \"episodes\": $EPISODES}" > $HERE/last-run.meta.json
cat $HERE/last-run.meta.json; cat $HERE/last-run.json
aws ec2 describe-instances --instance-ids $iid --query 'Reservations[0].Instances[0].State.Name' --output text
