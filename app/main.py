from fastapi import FastAPI
from pydantic import BaseModel, Field
from .database import SessionLocal
from .models import SupportQuery


app = FastAPI(title = "SupportIq")

#**structure what we get from customer**

class SupportRequest(BaseModel):
    customer_email : str
    question : str = Field(min_length = 1 , max_length = 500)


#temporary store the queries later we integrate with PostgreSQL
queries = {}

@app.get("/")
def home():
    return {"message": "SupportIq is running"}

# to create  unique query_id 
@app.post("/queries", status_code = 202)
def create_query(request: SupportRequest):
    db = SessionLocal()
    query = SupportQuery(question= request.question,
                         customer_email=request.customer_email)

    db.add(query)
    db.commit()
    db.refresh(query)
    db.close()


    return {"query_id":str(query.id), "status":query.status}

    
