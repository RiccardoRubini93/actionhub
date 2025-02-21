#!/bin/bash
set -e

ENV=$1  # Pass "dev" or "prod" as an argument

echo "Deploying to $ENV"

if [[ -z "$ENV" ]]; then
  echo "Usage: ./deploy.sh <dev|prod>"
  exit 1
fi

# Create a temporary copy of service.yaml
cp service_c4m_v2.yaml service-temp.yaml

# Replace all instances of ${ENVIRONMENT} with the actual value
sed -i "s/\${ENV}/$ENV/g" service-temp.yaml

# Deploy the updated service.yaml to Cloud Run
gcloud run services replace service-temp.yaml

rm service-temp.yaml
