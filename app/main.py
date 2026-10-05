from fastapi import FastAPI
from pydantic import BaseModel, Field
from uuid import UUID, uuid4
from datetime import datetime


app = FastAPI(title = "SupportIq")

#**structure what we get from customer**

class SupportRequest(BaseModel):
    customer_id : str = Field(min_length = 1)
    email : str
    message : str = Field(min_length = 5 , max_length = 500)


#temporary store the queries later we integrate with PostgreSQL
queries = {}

@app.get("/")
def home():
    return {"message": "SupportIq is running"}

# to create  unique query_id 
@app.post("/queries", status_code = 202)
def create_query(request: SupportRequest):
    query_id = str(uuid4())

    queries[query_id] = {
        "customer_id":request.customer_id,
        "email":request.email,
        "message": request.message,
        "status": "pending",
        "created_at":datetime.utcnow().isoformat(),
    }

    return {"query_id":query_id, "status":"pending"}

