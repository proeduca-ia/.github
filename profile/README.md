# Departamento de IA — PROEDUCASUMMAS

Desarrollamos los sistemas de inteligencia artificial transversales a todos los productos educativos del grupo.

## Repositorios principales

| Repositorio | Descripción |
|---|---|
| [ia-core-python](https://github.com/proeduca-ia/ia-core-python) | Librería compartida — stack Python |
| [ia-core-node](https://github.com/proeduca-ia/ia-core-node) | Librería compartida — stack Node.js/TypeScript |
| [ia-service-template-python](https://github.com/proeduca-ia/ia-service-template-python) | Plantilla de microservicio Python ★ |
| [ia-service-template-node](https://github.com/proeduca-ia/ia-service-template-node) | Plantilla de microservicio Node.js ★ |
| [ia-infra](https://github.com/proeduca-ia/ia-infra) | Infraestructura (IaC, entornos, scripts) |
| [ia-experiments](https://github.com/proeduca-ia/ia-experiments) | Experimentos y pruebas de concepto |
| [proeduca-ia](https://github.com/proeduca-ia/proeduca-ia) | Documentación y gobernanza del departamento |

## Stacks soportados

- **Python** — FastAPI + LangChain/LangGraph, `ia-core-python`
- **Node.js/TypeScript** — Express/Fastify + LangChain.js, `ia-core-node`

## Principios de diseño

- Arquitectura de puertos y adaptadores (`core/` no importa de `infra/`)
- Observabilidad de tokens y costes LLM mediante factoría centralizada
- Prompts como artefactos versionados en `src/infra/prompts/`
- Validación en boundary de entrada (Pydantic / Zod)
- Secretos solo en `.env`, nunca en código ni logs

---

Para empezar: consulta el [Engineering Handbook](https://github.com/proeduca-ia/proeduca-ia/blob/main/docs/04-desarrollo/04-01-engineering-handbook.md).
