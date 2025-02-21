#!/bin/bash

SOURCE_PROJECT="dev-cross-cloud4marketing"
DEST_PROJECT="c4m-customer-activation"

# Get a list of secret names from the source project
SECRETS=$(gcloud secrets list --project="$SOURCE_PROJECT" --format="value(name)")

for SECRET in $SECRETS; do
    echo "Cloning secret: $SECRET"

    # Get the latest version of the secret
    SECRET_VALUE=$(gcloud secrets versions access latest --secret="$SECRET" --project="$SOURCE_PROJECT")

    # Create the secret in the destination project
    gcloud secrets create "${SECRET}_dev" --replication-policy="automatic" --project="$DEST_PROJECT"

    # Add the secret value to the destination project
    echo -n "$SECRET_VALUE" | gcloud secrets versions add "${SECRET}_dev" --data-file=- --project="$DEST_PROJECT"

    echo "Secret $SECRET cloned successfully!"
done
