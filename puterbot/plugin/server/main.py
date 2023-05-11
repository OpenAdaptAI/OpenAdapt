import uvicorn
from typing import List, Dict
from fastapi import FastAPI, File, Form, HTTPException, Body
from starlette.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
# import datetime, maybe for timestamps?
from data.demo import (
    sendkeys
)


# USEFUL : https://github.com/NextWordDev/chatgpt-plugins/tree/main 

app = FastAPI()

PORT = 3333

origins = [
    f"http://localhost:{PORT}",
    "https://chat.openai.com",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _automate_helper(data):
    out = sendkeys(data)
    return out


#@app.post("/automate", response_model=Dict)
@app.post("/")
async def automate(data: str):
    return _automate_helper(data)


@app.route("/.well-known/ai-plugin.json")
async def get_manifest(request):
    file_path = "./server/ai-plugin.json"
    return FileResponse(file_path, media_type="text/json", headers={"Cache-Control": "no-store"})


@app.route("/.well-known/logo.png")
async def get_logo(request):
    file_path = "./server/logo.png"
    return FileResponse(file_path, media_type="text/json", headers={"Cache-Control": "no-store"})


@app.route("/.well-known/openapi.yaml")
async def get_openapi(request):
    file_path = "./server/openapi.yaml"
    return FileResponse(file_path, media_type="text/json", headers={"Cache-Control": "no-store"})


@app.on_event("startup")
async def startup():
    print("Starting up...")


def start():
    uvicorn.run("server.main:app", host="localhost", port=PORT, reload=True)