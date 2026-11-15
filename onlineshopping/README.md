# ShopSphere - Kubernetes Microservices Workshop (EBS version)

A hands-on shopping application for demonstrating Kubernetes microservices, product catalogue management, purchases, PostgreSQL, NGINX Ingress, and persistent EBS storage on Amazon EKS.

## Application features

- Image-based catalogue (laptop, headphones, keyboard, smartwatch, camera); images are loaded from public URLs.
- Add, list, update, and delete products, including price, stock, description, and image URL.
- Place orders, reduce stock, and view order history.
- PostgreSQL stores authoritative product, order, and order-item records on an EBS-backed PVC.
- Product API stores a catalogue JSON snapshot on its own EBS-backed PVC.
- Order API stores order receipt JSON files on its own EBS-backed PVC.

## Storage architecture

```text
                         Amazon EKS
     +------------------------------------------------+
     |                                                |
     |  PostgreSQL Pod ---- postgres-data-pvc --------+---- EBS gp3 volume (20 GiB)
     |                                                |
     |  Product API Pod -- product-api-data-pvc ------+---- EBS gp3 volume (5 GiB)
     |                                                |
     |  Order API Pod ---- order-api-data-pvc --------+---- EBS gp3 volume (5 GiB)
     |                                                |
     +------------------------------------------------+

PVC (PersistentVolumeClaim) -> EBS CSI Driver -> PV (PersistentVolume) -> EBS volume
```

**Important EBS behavior:** EBS volumes use `ReadWriteOnce` in this design and are AZ-scoped. Each workload has its own PVC and one replica. Do not mount one EBS volume as shared storage for Product API and Order API Pods on different nodes. PostgreSQL remains the source of truth; the API JSON files are separate local snapshots/receipts, not shared files. For genuinely shared POSIX file storage, use Amazon EFS instead.

## Project structure

```text
├── frontend/                      # Shopping UI
├── product-api/                   # Product CRUD + catalogue snapshot
├── order-api/                     # Checkout + order receipts
└── k8s/
    ├── configmap.yaml
    ├── storageclass-ebs-gp3.yaml   # Dynamic gp3 EBS provisioning
    ├── pvc.yaml                   # PVCs for PostgreSQL and both APIs
    ├── deployments.yaml           # Deployments mount their PVCs
    ├── ingress.yaml
    └── storage-volumes-demo.yaml  # Optional standalone EBS PVC demo
```

## 1. Check EKS cluster and EBS CSI driver

The EBS CSI driver is required for Kubernetes to provision and attach EBS volumes. Set your cluster details in Git Bash:

```bash
export AWS_REGION=ap-south-1
export CLUSTER_NAME=application-eks
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

Check whether the add-on already exists:

```bash
aws eks describe-addon --cluster-name "$CLUSTER_NAME" \
  --addon-name aws-ebs-csi-driver --region "$AWS_REGION"
```

If it is not installed, associate the cluster OIDC provider and create the IAM role/service account for the driver:

```bash
 eksctl utils associate-iam-oidc-provider \
  --cluster "$CLUSTER_NAME" --region "$AWS_REGION" --approve

 eksctl create iamserviceaccount \
  --cluster "$CLUSTER_NAME" \
  --region "$AWS_REGION" \
  --namespace kube-system \
  --name ebs-csi-controller-sa \
  --role-name AmazonEKS_EBS_CSI_DriverRole \
  --attach-policy-arn arn:aws:iam::aws:policy/service-role/AmazonEBSCSIDriverPolicy \
  --approve
```

Install the managed add-on using the role created above:

```bash
aws eks create-addon \
  --cluster-name "$CLUSTER_NAME" \
  --addon-name aws-ebs-csi-driver \
  --service-account-role-arn "arn:aws:iam::${ACCOUNT_ID}:role/AmazonEKS_EBS_CSI_DriverRole" \
  --region "$AWS_REGION"
```

If the add-on is already installed, do not recreate the IAM role blindly. Check its status and role association first. Verify the driver:

```bash
kubectl get pods -n kube-system | grep ebs-csi
kubectl get csidriver ebs.csi.aws.com
```

## 2. Build and push application images

Replace `YOUR_DOCKERHUB_USER` with your registry account. The image names in `k8s/deployments.yaml` must match the images you push.

```bash
docker login
docker build -t YOUR_DOCKERHUB_USER/online-shop:frontend-v3 ./frontend
docker build -t YOUR_DOCKERHUB_USER/online-shop:product-v3 ./product-api
docker build -t YOUR_DOCKERHUB_USER/online-shop:order-v3 ./order-api

docker push YOUR_DOCKERHUB_USER/online-shop:frontend-v3
docker push YOUR_DOCKERHUB_USER/online-shop:product-v3
docker push YOUR_DOCKERHUB_USER/online-shop:order-v3
```

Update the three `image:` entries in `k8s/deployments.yaml`. Keep `devpipeline/online-shop` only if those tags exist in a repository you control.

## 3. Create namespace, StorageClass, and PVCs

```bash
kubectl create namespace microservices-demo --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -f k8s/storageclass-ebs-gp3.yaml
kubectl apply -f k8s/pvc.yaml
```

The three PVCs request:

| PVC | Requested size | Used by |
|---|---:|---|
| `postgres-data-pvc` | 20 GiB | PostgreSQL data directory |
| `product-api-data-pvc` | 5 GiB | Product catalogue snapshot |
| `order-api-data-pvc` | 5 GiB | Order receipt files |

The StorageClass uses `WaitForFirstConsumer`. A PVC may remain `Pending` until its Pod is scheduled; this is expected. The EBS CSI driver then creates an EBS volume in the Availability Zone selected for the Pod and dynamically creates the corresponding Kubernetes PV.

Inspect provisioning:

```bash
kubectl get storageclass
kubectl get pvc -n microservices-demo
kubectl get pv
```

You do not write a static `PersistentVolume` manifest for this dynamic-provisioning setup. The CSI driver creates each PV automatically from its PVC. This is the recommended approach for a normal EKS application. The StorageClass reclaim policy is `Retain`, so deleting a PVC does not automatically delete its EBS volume; clean up retained volumes deliberately in the AWS Console after confirming you no longer need the data.

## 4. Configure application Secret and ConfigMap

```bash
kubectl apply -f k8s/configmap.yaml
kubectl create secret generic shopping-secret \
  --namespace microservices-demo \
  --from-literal=POSTGRES_PASSWORD='CHANGE_ME' \
  --from-literal=DATABASE_URL='postgresql://shopuser:CHANGE_ME@postgres:5432/shop' \
  --dry-run=client -o yaml | kubectl apply -f -
```

Use the same password in both values. Replace `CHANGE_ME` with a strong workshop password.

## 5. Deploy the application

```bash
kubectl apply -f k8s/deployments.yaml
kubectl apply -f k8s/ingress.yaml
kubectl get pods -n microservices-demo -o wide
kubectl get pvc -n microservices-demo
kubectl get pv
kubectl get svc,ingress -n microservices-demo
```

An NGINX Ingress Controller with IngressClass `nginx` must already be installed. The Ingress routes to `frontend-service`; frontend NGINX proxies `/product` to `product-service` and `/order` to `order-service`.

For workstation access, map the NGINX Ingress Controller external IP to `myfirst-web.com` in `C:\Windows\System32\drivers\etc\hosts`:

```text
<NGINX_INGRESS_EXTERNAL_IP> myfirst-web.com
```

Open `http://myfirst-web.com`.

## 6. Workshop demonstrations

### Demo A - Persistent PostgreSQL data

1. Add or edit a product in the UI.
2. Check the PostgreSQL PVC and dynamically provisioned PV:

```bash
kubectl get pvc -n microservices-demo
kubectl get pv
kubectl describe pvc postgres-data-pvc -n microservices-demo
```

3. Delete the PostgreSQL Pod (not the PVC) and show Kubernetes recreating it with the same database data:

```bash
kubectl delete pod -n microservices-demo -l app=postgres
kubectl get pods -n microservices-demo -w
```

After it becomes Ready, refresh the application and confirm the product/order data remains.

### Demo B - API file persistence

```bash
kubectl exec -n microservices-demo deployment/product-api -- ls -l /shared/catalog
kubectl exec -n microservices-demo deployment/product-api -- cat /shared/catalog/products.json
kubectl exec -n microservices-demo deployment/order-api -- ls -l /shared/order-receipts
```

Place an order in the UI, then inspect the receipt in the Order API Pod. Product and Order API volumes are separate; the JSON files are not shared between these Pods.

### Demo C - Inspect EBS-backed PVs

```bash
kubectl get pvc -n microservices-demo -o wide
kubectl get pv -o wide
kubectl describe pv <PV_NAME>
```

In AWS Console, open **EC2 -> Elastic Block Store -> Volumes** and match the EBS volume ID shown in the PV details. Demonstrate its size, type (`gp3`), AZ, and encryption status.

## 7. Optional standalone EBS PV/PVC lab

After the application PVCs are working, you can demonstrate a separate claim and Pod:

```bash
kubectl apply -f k8s/storage-volumes-demo.yaml
kubectl get pvc,pod -n microservices-demo
kubectl get pv
```

Write a file:

```bash
kubectl exec -n microservices-demo ebs-volume-demo -- sh -c 'echo EBS-persistent-data >> /data/demo.txt'
kubectl exec -n microservices-demo ebs-volume-demo -- cat /data/demo.txt
```

Delete only the Pod and recreate the Pod from the manifest; the file remains because the PVC and EBS volume remain. Delete the demo PVC only when finished, then handle the retained EBS volume according to your cleanup plan.

## Important notes

- EBS volumes are AZ-scoped. A volume cannot be attached to a worker node in another AZ. `WaitForFirstConsumer` lets Kubernetes choose a compatible AZ when it provisions the volume.
- EBS `ReadWriteOnce` is not shared NFS storage. This project therefore gives PostgreSQL, Product API, and Order API separate claims and keeps each of those Deployments at one replica. Frontend can scale independently because it does not need a PVC.
- Do not scale PostgreSQL beyond one replica or attach the same PGDATA volume to multiple PostgreSQL Pods.
- The product image binaries are not stored on EBS; the app stores image URLs in PostgreSQL.
- For production, consider Amazon RDS for PostgreSQL, and EFS if multiple application Pods need shared filesystem access.
