FROM golang:1.23-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/coworker-relay ./cmd/coworker-relay \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/relayctl ./cmd/relayctl

FROM alpine:3.22
RUN apk add --no-cache ca-certificates \
    && addgroup -S relay \
    && adduser -S -G relay relay
COPY --from=build /out/coworker-relay /usr/local/bin/coworker-relay
COPY --from=build /out/relayctl /usr/local/bin/relayctl
RUN mkdir -p /var/lib/coworker-relay && chown relay:relay /var/lib/coworker-relay
USER relay
VOLUME ["/var/lib/coworker-relay"]
EXPOSE 8080 8443
ENTRYPOINT ["coworker-relay"]
