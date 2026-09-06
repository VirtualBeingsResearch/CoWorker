FROM golang:1.27.1-alpine AS build
ARG RELAY_VERSION=dev
ARG RELAY_COMMIT=unknown
ARG RELAY_BUILD_DATE=unknown
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN ldflags="-s -w \
      -X github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/buildinfo.Version=${RELAY_VERSION} \
      -X github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/buildinfo.Commit=${RELAY_COMMIT} \
      -X github.com/VirtualBeingsResearch/CoWorker/apps/coworker-relay/internal/buildinfo.Date=${RELAY_BUILD_DATE}" \
    && CGO_ENABLED=0 go build -trimpath -ldflags="$ldflags" -o /out/coworker-relay ./cmd/coworker-relay

FROM alpine:3.24
# The floating `alpine:3.24` tag can lag the package index, shipping an older
# (vulnerable) OpenSSL. Upgrade to the branch's current packages at build time so
# the scan sees the fixed libcrypto3/libssl3 (e.g. CVE-2026-14456).
RUN apk upgrade --no-cache \
    && apk add --no-cache ca-certificates \
    && addgroup -S relay \
    && adduser -S -G relay relay
COPY --from=build /out/coworker-relay /usr/local/bin/coworker-relay
RUN mkdir -p /var/lib/coworker-relay && chown relay:relay /var/lib/coworker-relay
USER relay
VOLUME ["/var/lib/coworker-relay"]
EXPOSE 8443 8444
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 CMD ["coworker-relay", "health"]
ENTRYPOINT ["coworker-relay"]
CMD ["serve"]
