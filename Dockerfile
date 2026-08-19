# Официальный образ Playwright — браузер и все системные зависимости для
# headless Chromium уже внутри, протестированы производителем. Версия тут
# ДОЛЖНА совпадать с playwright==... в requirements.txt, иначе Playwright не
# найдёт браузер (см. requirements.txt).
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "bot.py"]
