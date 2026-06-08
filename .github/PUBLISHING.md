# Publishing to GHCR

Images are published to `ghcr.io/srwareham/trmnl-huckleberry` via GitHub Actions and can also be pushed manually.

## Automated publishing (GitHub Actions)

The workflow at `.github/workflows/publish.yml` triggers automatically:

| Trigger | Tags produced |
|---------|--------------|
| Push to `main` | `latest`, `sha-<short-sha>` |
| Push of `v1.2.3` tag | `1.2.3`, `1.2`, `1`, `latest`, `sha-<short-sha>` |

Pushing the same tag again **overwrites** it — GHCR does not error on re-push, so all operations are idempotent.

### Releasing a version

```sh
git tag v1.2.3
git push origin v1.2.3
```

The workflow runs, builds for `linux/amd64` and `linux/arm64`, and pushes all tags within a few minutes. To verify:

```sh
gh run watch   # or: gh run list
```

To undo a bad release, delete the tag locally and remotely — the image in GHCR is not automatically removed, but you can delete it via the GitHub UI (Packages → trmnl-huckleberry → manage versions).

## Manual publishing

Use this when you need to push without a CI run (e.g., testing the image name, one-off builds).

### Prerequisites

```sh
# Authenticate once per machine (token needs write:packages scope)
echo $GITHUB_TOKEN | docker login ghcr.io -u srwareham --password-stdin
```

### Build and push

```sh
IMAGE=ghcr.io/srwareham/trmnl-huckleberry

# Build multi-platform and push in one step (requires buildx)
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --tag "$IMAGE:latest" \
  --push \
  .
```

To also push a version tag alongside `latest`:

```sh
VERSION=1.2.3
docker buildx build \
  --platform linux/amd64,linux/arm64 \
  --tag "$IMAGE:latest" \
  --tag "$IMAGE:$VERSION" \
  --push \
  .
```

Running the same command again is safe — it overwrites the tags with an identical or updated image.

### Verify the pushed image

```sh
docker buildx imagetools inspect ghcr.io/srwareham/trmnl-huckleberry:latest
```

This shows the manifest list and confirms both `linux/amd64` and `linux/arm64` are present.

## Package visibility

New packages on GHCR default to **private**. To make the image public (so users can pull without authentication):

1. Go to **github.com/srwareham** → **Packages** → `trmnl-huckleberry`
2. **Package settings** → **Change visibility** → **Public**

This only needs to be done once. Future pushes inherit the visibility.
