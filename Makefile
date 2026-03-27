# AgentKit — Makefile (rule 43)

.PHONY: dev test logs shell add-tenant install

# Start server with hot reload
dev:
	uvicorn agent.main:app --reload --port 8000

# Run all tests
test:
	pytest tests/ -v

# Tail Docker logs
logs:
	docker compose logs -f agent

# Open Python shell with app context
shell:
	python -c "import asyncio; from agent.memory import inicializar_db; asyncio.run(inicializar_db()); import code; code.interact(local=locals())"

# Install dependencies
install:
	pip install -r requirements.txt

# Interactive script to add a new tenant
add-tenant:
	@echo "=== Agregar nuevo tenant ==="
	@read -p "Número WhatsApp Business (ej: +54911XXXXXXXX): " phone; \
	read -p "ID del tenant (ej: salon_bella): " tid; \
	read -p "Nombre del negocio: " name; \
	read -p "Número del dueño: " owner; \
	echo "  $$phone:" >> config/tenants.yaml; \
	echo "    id: \"$$tid\"" >> config/tenants.yaml; \
	echo "    name: \"$$name\"" >> config/tenants.yaml; \
	echo "    locale: \"es-AR\"" >> config/tenants.yaml; \
	echo "    prompts_file: \"config/prompts.yaml\"" >> config/tenants.yaml; \
	echo "    knowledge_dir: \"knowledge/$$tid/\"" >> config/tenants.yaml; \
	echo "    db_url: \"sqlite+aiosqlite:///data/$$tid.db\"" >> config/tenants.yaml; \
	echo "    owner_phone: \"$$owner\"" >> config/tenants.yaml; \
	echo "    business_hours: {start: \"09:00\", end: \"20:00\", timezone: \"America/Argentina/Buenos_Aires\", days: [1,2,3,4,5]}" >> config/tenants.yaml; \
	echo "    escalation_cooldown_minutes: 30" >> config/tenants.yaml; \
	echo "    max_history_messages: 20" >> config/tenants.yaml; \
	echo "    summary_threshold: 40" >> config/tenants.yaml; \
	echo "✅ Tenant $$tid agregado a config/tenants.yaml"
