from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.exceptions import HTTPException
from app.config import CONFIG
from app.services.account_service import account_service

async def check_single_account(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    
    try:
        body = await request.json()
        account = body.get("account")
        if not account:
            return JSONResponse({"detail": "Missing account"}, status_code=400)
    except:
         return JSONResponse({"detail": "Invalid JSON"}, status_code=400)
    
    result = await account_service.check_account(account)
    return JSONResponse(result)

async def delete_account(request: Request):
    token = request.headers.get("X-Admin-Token")
    if token != CONFIG.ADMIN_TOKEN:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        
    try:
        body = await request.json()
        account = body.get("account")
        if not account:
            return JSONResponse({"detail": "Missing account"}, status_code=400)
    except:
         return JSONResponse({"detail": "Invalid JSON"}, status_code=400)

    deleted = await account_service.delete_session(account)
    return JSONResponse({"account": account, "deleted": deleted})
