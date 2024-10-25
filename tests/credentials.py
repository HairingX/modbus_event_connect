from typing import List


try:
    #try to import from mysecrets.py, if it exists
    from mysecrets import USERNAME, PASSWORD, ACCESS_TOKEN, REFRESH_TOKEN, TOKEN_TYPE, LOCATION_ID, HOSTNAME
    U = USERNAME
    P = PASSWORD
    AT = ACCESS_TOKEN
    RT = REFRESH_TOKEN
    TT = TOKEN_TYPE
    LOC_ID = LOCATION_ID
    HOST = HOSTNAME
except ModuleNotFoundError:
    pass

class Credentials:
    # types will be str (not None) as we are checking for required fields in __init__
    access_token: str
    hostname: str
    location_id: str
    password: str
    refresh_token: str
    token_type: str
    username: str
    
    def __init__(self, required: List[str]) -> None:
        required = [item.upper() for item in required]
        
        if 'ACCESS_TOKEN' in globals():
            self.access_token = globals()['ACCESS_TOKEN'] 
        elif 'ACCESS_TOKEN' in required:
            raise ValueError("ACCESS_TOKEN not found in secrets.py")
            
        if 'HOSTNAME' in globals():
            self.hostname = globals()['HOSTNAME']
        elif 'HOSTNAME' in required:
            raise ValueError("HOSTNAME not found in secrets.py")
            
        if 'LOCATION_ID' in globals():
            self.location_id = globals()['LOCATION_ID']
        elif 'LOCATION_ID' in required:
            raise ValueError("LOCATION_ID not found in secrets.py")
        
        if 'PASSWORD' in globals():
            self.password = globals()['PASSWORD']
        elif 'PASSWORD' in required:
            raise ValueError("PASSWORD not found in secrets.py")
        
        if 'REFRESH_TOKEN' in globals():
            self.refresh_token = globals()['REFRESH_TOKEN']
        elif 'REFRESH_TOKEN' in required:
            raise ValueError("REFRESH_TOKEN not found in secrets.py")
        
        if 'TOKEN_TYPE' in globals():
            self.token_type = globals()['TOKEN_TYPE']
        elif 'TOKEN_TYPE' in required:
            raise ValueError("TOKEN_TYPE not found in secrets.py")
        
        if 'USERNAME' in globals():
            self.username = globals()['USERNAME']
        elif 'USERNAME' in required:
            raise ValueError("USERNAME not found in secrets.py")
