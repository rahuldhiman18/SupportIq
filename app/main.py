from fastapi import FastAPI
from pydantic import BaseModel


app = FastAPI()

class SupportRequest(BaseModel):
    question : str

@app.post("/SupportRequest/")
def create_query(supportrequest: SupportRequest):
    return {"status":"success",
        "question":supportrequest.question,
        "query_id":supportrequest.query_id}

