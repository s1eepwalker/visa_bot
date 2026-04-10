import httpx, re
r = httpx.get(
    'https://ais.usvisa-info.com/ru-kz/niv/users/sign_in',
    headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36',
        'Accept': 'text/html',
    }
)
i = r.text.find('authenticity')
print(r.text[i-100:i+300])