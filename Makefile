SIMULATOR ?= iPhone Air
DERIVED := .build/DerivedData
APP := $(DERIVED)/Build/Products/Debug-iphonesimulator/Steer.app

.PHONY: help project build test helper-test run-sim clean

help:
	@echo "make project      regenerate Steer.xcodeproj (xcodegen)"
	@echo "make build        build for the Simulator"
	@echo "make test         run the app's unit tests in the Simulator"
	@echo "make helper-test  run the host helper's unit tests"
	@echo "make run-sim      build, install and launch in the Simulator"

project:
	xcodegen generate --quiet

build: project
	xcodebuild -project Steer.xcodeproj -scheme Steer -destination 'platform=iOS Simulator,name=$(SIMULATOR)' \
		-derivedDataPath $(DERIVED) -quiet build

test: project
	xcodebuild -project Steer.xcodeproj -scheme Steer -destination 'platform=iOS Simulator,name=$(SIMULATOR)' \
		-derivedDataPath $(DERIVED) -quiet test

helper-test:
	python3 -m unittest discover -s helper/tests -p 'test_*.py'

run-sim: build
	xcrun simctl boot '$(SIMULATOR)' 2>/dev/null || true
	xcrun simctl install '$(SIMULATOR)' $(APP)
	xcrun simctl launch '$(SIMULATOR)' com.kadaliao.steer

clean:
	rm -rf .build Steer.xcodeproj
