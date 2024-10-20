# C4M Action Hub

The steps to deploy the Action Hub in a Cloud Run are the following:

1. Set the project: gcloud config set project [PROJECT_ID]
2. Authenticate like the service account: <code>gcloud auth activate-service-account --key-file=[SERVICE_ACCOUNT_KEY_FILE].json</code> or use your own credentials.
3. Go to the root directory of the repository and execute the following command: <code>gcloud run deploy looker-actionhub --region=europe-west1 --source . --allow-unauthenticated </code>