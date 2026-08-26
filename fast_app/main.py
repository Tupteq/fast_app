"""FastAPI application and its AWS Lambda handler."""

import fastapi
import mangum

app = fastapi.FastAPI()


@app.get("/")
async def root():
    return {"message": "Hello from FastAPI!"}


handler = mangum.Mangum(app, lifespan="off")
