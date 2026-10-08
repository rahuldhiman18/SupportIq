from datetime import datetime
from uuid import UUID

from fastapi import FastAPI,Depends,HTTPException
from pydantic import BaseModel, Field
from .database import SessionLocal
from .models import SupportQuery
from sqlalchemy import select

app = FastAPI(title = "SupportIq")

#**structure what we get from customer**

class SupportRequest(BaseModel):
    customer_email : str
    question : str = Field(min_length = 1 , max_length = 500)

class QueryResponse(BaseModel):
    query_id:UUID
    question: str
    status: str
    created_at:datetime

class QueryUpdate(BaseModel):
    status: str



def get_db():
    db = SessionLocal()

    try:
       yield db
    finally:
       db.close()

# to create  unique query_id 
@app.post("/queries", status_code = 202,response_model=QueryResponse)
def create_query(request: SupportRequest,db = Depends(get_db)):
    query = SupportQuery(question= request.question,
                         customer_email=request.customer_email)
    db.add(query)
    db.commit()
    db.refresh(query)
    return {
    "query_id": query.id,
    "question": query.question,
    "status": query.status,
    "created_at": query.created_at
}

@app.get("queries/{query_id}",response_model=QueryResponse)
def get_query(query_id:UUID,db = Depends(get_db)):
    result = db.execute(select(SupportQuery).where(SupportQuery.id == query_id))
    query = result.scalar_one_or_none()
    if query is None:
        raise HTTPException(status_code=404,detail="Query not found")
    return {"query_id":query.id,
            "question":query.question,
            "status":query.status,
            "created_at":query.created_at
            }


@app.patch("/queries/{query_id}",response_model=QueryResponse,status_code=200)
def update_query(query_id:UUID, request:QueryUpdate, db = Depends(get_db)):
    result = db.execute(select(SupportQuery).where(SupportQuery.id==query_id))
    query = result.scalar_one_or_none()
    if query is None:
        raise HTTPException(status_code= 404,detail="Query Not found")
    query.status = request.status
    db.commit()
    db.refresh(query)
    return {"query_id":query.id,
            "question":query.question,
            "status":query.status,
            "created_at":query.created_at
            }
    

    
    

    
