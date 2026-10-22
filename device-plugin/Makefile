BINARY      = kraken-device-plugin
IMAGE       = ketidevit2/kraken-device-plugin
TAG        ?= latest
GOFLAGS     = -trimpath -ldflags="-s -w"

.PHONY: build image push clean

build:
	CGO_ENABLED=0 GOOS=linux GOARCH=amd64 \
	    go build $(GOFLAGS) -o $(BINARY) .

image: build
	docker build -t $(IMAGE):$(TAG) -f deploy/Dockerfile .

push: image
	docker push $(IMAGE):$(TAG)

clean:
	rm -f $(BINARY)
