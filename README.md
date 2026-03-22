# API TAMS 2000

La version activa del proyecto esta en `python_service/`.

Es un microservicio en Python con `FastAPI` que:

- consulta `http://www.tams.com.ar/organismos/vuelos.aspx`
- guarda snapshots en SQLite
- ejecuta una sincronizacion automatica cada 6 horas

## Documentacion

La documentacion funcional y tecnica del sistema esta en [python_service/README.md](python_service/README.md).

## Endpoints

- `GET /health`
- `GET /latest`
- `GET /live`
- `POST /sync`

## Ejecutar

```powershell
cd .\python_service
.\.venv\Scripts\python -m uvicorn main:app --host 0.0.0.0 --port 8000
```
