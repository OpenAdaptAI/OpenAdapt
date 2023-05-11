from fastapi import FastAPI, Depends, Request, Form, UploadFile, status
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from sqlalchemy.orm import Session

import models
from db import Session, engine

models.Base.metadata.create_all(bind=engine)

templates = Jinja2Templates(directory="templates")
app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")
# Dependency
def get_db():
    db = Session()
    try:
        yield db
    finally:
        db.close()


@app.get("/")
def home(request: Request):
    return templates.TemplateResponse("index.html",{"request": request})


# @app.post("/share_automations")
# def upload(recording: UploadFile = File(...)):
#     pass
