BINARY_NAME := agentcore-sandbox
DIST_DIR := dist
GO_CMD := go build
SRC := ./cmd/agentcore-sandbox

PLATFORMS := darwin-arm64 darwin-amd64 linux-arm64 linux-amd64 windows-arm64 windows-amd64

.PHONY: build build-all clean $(PLATFORMS)

# Build for current platform
build:
	$(GO_CMD) -o $(BINARY_NAME) $(SRC)

# Build all platforms
build-all: $(PLATFORMS)

darwin-arm64:
	GOOS=darwin GOARCH=arm64 $(GO_CMD) -o $(DIST_DIR)/$(BINARY_NAME)-darwin-arm64 $(SRC)

darwin-amd64:
	GOOS=darwin GOARCH=amd64 $(GO_CMD) -o $(DIST_DIR)/$(BINARY_NAME)-darwin-amd64 $(SRC)

linux-arm64:
	GOOS=linux GOARCH=arm64 $(GO_CMD) -o $(DIST_DIR)/$(BINARY_NAME)-linux-arm64 $(SRC)

linux-amd64:
	GOOS=linux GOARCH=amd64 $(GO_CMD) -o $(DIST_DIR)/$(BINARY_NAME)-linux-amd64 $(SRC)

windows-arm64:
	GOOS=windows GOARCH=arm64 $(GO_CMD) -o $(DIST_DIR)/$(BINARY_NAME)-windows-arm64.exe $(SRC)

windows-amd64:
	GOOS=windows GOARCH=amd64 $(GO_CMD) -o $(DIST_DIR)/$(BINARY_NAME)-windows-amd64.exe $(SRC)

clean:
	rm -rf $(DIST_DIR) $(BINARY_NAME)
