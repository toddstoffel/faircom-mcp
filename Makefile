.PHONY: format lint typecheck test test-cov test-integration test-edge \
	container-build container-run compose-up compose-down package-verify package-build package-validate \
	release-integrity

format:
	python3 -m ruff format src tests

lint:
	python3 -m ruff check src tests

typecheck:
	python3 -m mypy src

test:
	python3 -m pytest -m "not edge_integration"

test-cov:
	python3 -m pytest -m "not edge_integration" --cov=src/faircom_mcp --cov-report=term-missing

test-integration:
	python3 -m pytest -m "integration and not edge_integration"

test-edge:
	bash scripts/test_with_edge.sh

container-build:
	docker build -t faircom-mcp:local .

container-run:
	docker run --rm -p 8000:8000 \
		-e FAIRCOM_API_BASE_URL=$${FAIRCOM_API_BASE_URL:-http://host.docker.internal:8080} \
		-e FAIRCOM_API_TOKEN=$${FAIRCOM_API_TOKEN:-} \
		-e FAIRCOM_HTTP_HOST=0.0.0.0 \
		-e FAIRCOM_HTTP_PORT=8000 \
		-e FAIRCOM_TLS_VERIFY=$${FAIRCOM_TLS_VERIFY:-true} \
		-e FAIRCOM_SQL_ALLOWLIST=$${FAIRCOM_SQL_ALLOWLIST:-} \
		-e FAIRCOM_SQL_DENYLIST=$${FAIRCOM_SQL_DENYLIST:-} \
		faircom-mcp:local --transport http

compose-up:
	docker compose up --build -d

compose-down:
	docker compose down

package-verify:
	test -f packaging/systemd/faircom-mcp.service
	test -f packaging/systemd/faircom-mcp.env.example
	test -f packaging/logrotate/faircom-mcp
	test -f packaging/sysusers.d/faircom-mcp.conf
	test -f packaging/tmpfiles.d/faircom-mcp.conf
	test -f packaging/rpm/faircom-mcp.spec
	test -f packaging/deb/control
	test -f packaging/deb/postinst
	test -f packaging/deb/prerm
	test -f packaging/deb/postrm

package-build: package-verify
	bash scripts/build_linux_packages.sh

package-validate:
	bash scripts/validate_linux_packages.sh

release-integrity:
	bash scripts/generate_release_integrity.sh
