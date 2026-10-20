from app.core.database import engine, Base
from app.models import tables as models

Base.metadata.create_all(engine)