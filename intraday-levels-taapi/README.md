# Intraday Levels Taapi.io API

API для анализа внутридневных уровней поддержки и сопротивления с использованием Taapi.io.

## Описание

Этот проект предоставляет REST API для:
- Получения свечных данных через Taapi.io bulk API
- Анализа технических индикаторов (RSI, MACD, Bollinger Bands, VWAP, Pivot Points, Swing Points, ATR, EMA200)
- Поиска уровней поддержки и сопротивления с анализом конвергенции
- **Поиска лучших уровней для входа** с учетом множественных факторов
- Кэширования результатов для оптимизации производительности
- Обработки торговых сигналов (long/short контекст)
- Записи изменений цветов шариков (ball flips)
- Bulk операций для множественных запросов

## Установка

1. Клонируйте репозиторий:
```bash
git clone <repository-url>
cd intraday-levels-taapi
```

2. Создайте виртуальное окружение:
```bash
python -m venv venv
source venv/bin/activate  # Linux/Mac
# или
venv\Scripts\activate  # Windows
```

3. Установите зависимости:
```bash
pip install -r requirements.txt
```

4. Настройте переменные окружения:
```bash
cp env.example .env
# Отредактируйте .env файл
```

## Конфигурация

Создайте файл `.env` со следующими переменными:

```env
TAAPI_KEY=your_taapi_key_here

# Настройки
SYMBOLS=AVAXUSDT,ICPUSDT,ETHUSDT
TF_LIST=5m,15m,30m,1h,4h
RESULTS_5m=2000
RESULTS_15m=2000
RESULTS_30m=2000
RESULTS_1h=1500
RESULTS_4h=1000

# Таймауты/кэш
CACHE_TTL_SECONDS=90
HTTP_TIMEOUT_SECONDS=20
MAX_RETRIES=3
CONCURRENCY=4
```

## Запуск

```bash
python -m app.main
```

API будет доступно по адресу: http://localhost:8000

## API Endpoints

### Основные
- `GET /` - Информация о API
- `GET /health` - Проверка здоровья сервиса
- `GET /config` - Текущая конфигурация
- `GET /symbols` - Список символов
- `GET /timeframes` - Список таймфреймов

### Технический анализ
- `GET /klines/{symbol}/{interval}?limit=100` - Свечные данные
- `GET /rsi/{symbol}/{interval}?period=14` - RSI индикатор
- `GET /macd/{symbol}/{interval}?fast_period=12&slow_period=26&signal_period=9` - MACD индикатор
- `GET /bbands/{symbol}/{interval}?period=20&std_dev=2` - Bollinger Bands
- `GET /vwap/{symbol}/{interval}?limit=100` - VWAP (Volume Weighted Average Price)
- `GET /pivots/{symbol}/{interval}?limit=100` - Pivot Points (Пивотные точки)
- `GET /swings/{symbol}/{interval}?limit=100&left=2&right=2&tolerance=0.001` - Swing Points (Свинг-точки)
- `GET /atr/{symbol}/{interval}?period=14` - ATR (Average True Range)
- `GET /ema200/{symbol}/{interval}` - EMA200
- `GET /levels/{symbol}/{interval}?limit=100` - Уровни поддержки и сопротивления

### Поиск лучших уровней (НОВОЕ!)
- `GET /best-level/{symbol}/{side}?limit=200` - Поиск лучшего уровня для входа (long/short)

### Bulk операции
- `GET /bulk/klines?symbols=ETHUSDT,BTCUSDT&intervals=1h,4h` - Свечные данные для нескольких символов
- `GET /bulk/indicators/{symbol}/{interval}?indicators=rsi,macd,bbands` - Несколько индикаторов для символа
- `GET /bulk/levels?symbols=ETHUSDT,BTCUSDT&intervals=1h,4h` - Уровни S/R для нескольких символов

### Торговые сигналы
- `POST /levels/search` - Поиск уровней на основе контекста (long/short)
- `POST /ball-flip` - Запись изменения цвета шариков

### Управление кэшем
- `GET /cache/stats` - Статистика кэша
- `GET /cache/keys` - Список всех ключей кэша
- `POST /cache/clear` - Очистка кэша
- `DELETE /cache/{key}` - Удаление конкретного ключа
- `PUT /cache/ttl` - Обновление TTL кэша

## Pydantic Модели

### LevelSearchRequest
```python
class LevelSearchRequest(BaseModel):
    symbol: str                    # 'ETHUSDT'
    context: Literal["long","short"]
    origin_tf: Literal["30m","60m"] = "30m"
```

### BallFlip
```python
class BallFlip(BaseModel):
    symbol: str
    timeframe: Literal["30m","60m"]
    old_color: Literal["green","red"]
    new_color: Literal["green","red"]
    balls: Dict[str, Literal["green","red"]]
    ts: str
```

### APIResponse
```python
class APIResponse(BaseModel):
    success: bool
    data: Optional[Dict] = None
    error: Optional[str] = None
    timestamp: str
```

## TTLCache

Проект использует улучшенный TTL кэш с автоматической очисткой истекших записей:

```python
class TTLCache:
    def __init__(self, ttl_seconds: int = 60)
    def get(self, key: str) -> Optional[Any]
    def set(self, key: str, value: Any) -> None
    def clear(self) -> None
    def delete(self, key: str) -> bool
    def exists(self, key: str) -> bool
    def keys(self) -> List[str]
    def size(self) -> int
    def get_stats(self) -> Dict[str, Any]
    def update_ttl(self, new_ttl: int) -> None
    def get_with_ttl(self, key: str) -> Optional[Tuple[Any, float]]
```

## Taapi.io Bulk API

Проект использует эффективный bulk API для оптимизации запросов:

- **Batching**: Автоматическое разбиение на батчи по 10 запросов
- **Rate Limiting**: Встроенная обработка rate limit ошибок
- **Retry Logic**: Экспоненциальная задержка при повторных попытках
- **Error Handling**: Детальное логирование ошибок

## VWAP (Volume Weighted Average Price)

Проект включает расчет VWAP - важного индикатора для анализа объемного профиля:

```python
def session_vwap(ohlcv_rows):
    """Расчет VWAP (Volume Weighted Average Price)"""
    num, den = 0.0, 0.0
    for r in ohlcv_rows:
        p = (float(r['high']) + float(r['low']) + float(r['close'])) / 3.0
        v = float(r['volume'])
        num += p * v; den += v
    return num/den if den > 0 else None
```

VWAP автоматически включается в результаты уровней поддержки и сопротивления.

## Pivot Points (Пивотные точки)

Проект включает расчет классических пивотных точек:

```python
def classic_pivots(prev_high, prev_low, prev_close):
    """Расчет классических пивотных точек"""
    pp = (prev_high + prev_low + prev_close)/3.0
    r1 = 2*pp - prev_low
    s1 = 2*pp - prev_high
    r2 = pp + (prev_high - prev_low)
    s2 = pp - (prev_high - prev_low)
    return dict(PP=pp,R1=r1,S1=s1,R2=r2,S2=s2)
```

**Пивотные точки включают:**
- **PP (Pivot Point)** - центральная точка
- **R1, R2 (Resistance)** - уровни сопротивления
- **S1, S2 (Support)** - уровни поддержки

Пивотные точки автоматически включаются в результаты уровней поддержки и сопротивления.

## Swing Points (Свинг-точки)

Проект включает поиск свинг-точек и кластеризацию уровней:

```python
def swing_points(ohlcv, left=2, right=2):
    """Поиск свинг-точек (локальных максимумов и минимумов)"""
    highs, lows = [], []
    for i in range(left, len(ohlcv)-right):
        h = float(ohlcv[i]['high'])
        if all(h>=float(ohlcv[j]['high']) for j in range(i-left, i+right+1) if j!=i):
            highs.append((i, h))
        l = float(ohlcv[i]['low'])
        if all(l<=float(ohlcv[j]['low']) for j in range(i-left, i+right+1) if j!=i):
            lows.append((i, l))
    return highs, lows

def cluster_levels(points, tolerance):
    """Кластеризация уровней по толерантности"""
    if not points: 
        return []
    points = sorted(points)
    clusters, cur = [], [points[0]]
    for p in points[1:]:
        if abs(p - cur[-1]) <= tolerance:
            cur.append(p)
        else:
            clusters.append(sum(cur)/len(cur))
            cur = [p]
    clusters.append(sum(cur)/len(cur))
    return clusters
```

**Свинг-точки включают:**
- **Swing Highs** - локальные максимумы (сопротивление)
- **Swing Lows** - локальные минимумы (поддержка)
- **Clustered Levels** - кластеризованные уровни по толерантности
- **Настраиваемые параметры** - left, right, tolerance

Свинг-точки автоматически включаются в результаты уровней поддержки и сопротивления.

## Анализ конвергенции уровней

Проект включает продвинутый анализ конвергенции факторов для оценки силы уровней:

```python
def compute_tolerance(price, atr, tick):
    """Расчет толерантности для уровней на основе цены, ATR и тика"""
    atr = atr or 0.0
    return max(0.20*atr, price*0.0010, 3*tick)

def score_level(level_price, confluence, rr, smashed_recent=False):
    """Оценка силы уровня на основе конвергенции факторов"""
    weights = {
        "htf_swing": 0.18,      # Свинг-точки на старших таймфреймах
        "pivot": 0.12,          # Пивотные точки
        "ema200_near": 0.12,    # Близость к EMA200
        "vwap": 0.10,           # VWAP
        "round": 0.06,          # Круглые числа
        "touches": 0.18,        # Количество касаний
        "volume_rejection": 0.12, # Отклонение объемом
        "trend_ok": 0.12        # Соответствие тренду
    }
    s = sum(weights[k] for k in confluence if k in weights)
    if rr is not None and rr < 1.8: 
        return 0.0  # Слишком плохое соотношение риск/прибыль
    if smashed_recent: 
        return 0.0  # Уровень недавно пробит
    return min(s, 1.0)
```

**Факторы конвергенции:**
- **VWAP** - близость к VWAP (1% толерантность)
- **Pivot Points** - близость к пивотным точкам (0.5% толерантность)
- **EMA200** - близость к EMA200 (2% толерантность)
- **Round Numbers** - круглые числа
- **Touches** - количество касаний уровня
- **Trend Alignment** - соответствие тренду

**Расчет толерантности:**
- **ATR-based** - 20% от ATR
- **Price-based** - 0.1% от цены
- **Tick-based** - 3 тика

## Поиск лучших уровней (НОВОЕ!)

Проект включает продвинутый алгоритм поиска лучших уровней для входа:

```python
def find_best_level(c5, c15, c30, c1h, c4h, session_info,
                    tick_size, indicators, side, origin_tf="30m"):
    """Находит лучший уровень для входа на основе множественных факторов"""
    # Анализ свинг-точек на множественных таймфреймах
    # Кластеризация уровней с динамической толерантностью
    # Анализ конвергенции факторов
    # Расчет соотношения риск/прибыль
    # Оценка силы уровня
    # Возврат лучшего уровня с оценкой >= 0.72
```

**Ключевые особенности:**
- **Множественные таймфреймы** - анализ 5m, 15m, 30m, 1h, 4h
- **Динамическая толерантность** - на основе ATR, цены и тика
- **Конвергенция факторов** - VWAP, пивоты, EMA200, круглые числа
- **Соотношение риск/прибыль** - автоматический расчет RR
- **Минимальная оценка** - только уровни с оценкой >= 0.72
- **Анализ сессии** - PDH/PDL, дневные пивоты, VWAP сессии

**Функции поиска:**
- `infer_tick_from_price(price)` - определение размера тика на основе цены
- `nearest_round(price)` - поиск ближайшего круглого числа
- `rr_to_opposite(level_price, opposite_levels, entry_dir)` - расчет RR
- `analyze_session_info(candles_1h, candles_4h)` - анализ сессии
- `find_levels_for_side(symbol, side, taapi_service)` - поиск для стороны

**Определение размера тика:**
```python
def infer_tick_from_price(price: float) -> float:
    """Определяет приблизительный размер тика на основе цены"""
    if price >= 1000: return 0.1      # BTC, ETH и др.
    if price >= 100:  return 0.01     # AVAX, ICP и др.
    if price >= 10:   return 0.001    # DOGE, ADA и др.
    if price >= 1:    return 0.0001   # SHIB и др.
    return 0.00001                    # Очень дешевые токены
```

## Примеры использования

### Получение свечных данных
```bash
curl "http://localhost:8000/klines/BTCUSDT/1h?limit=100"
```

### Получение RSI
```bash
curl "http://localhost:8000/rsi/ETHUSDT/4h?period=14"
```

### Получение VWAP
```bash
curl "http://localhost:8000/vwap/ETHUSDT/1h?limit=100"
```

### Получение пивотных точек
```bash
curl "http://localhost:8000/pivots/ETHUSDT/1h?limit=100"
```

### Получение свинг-точек
```bash
curl "http://localhost:8000/swings/ETHUSDT/1h?limit=100&left=2&right=2&tolerance=0.001"
```

### Получение ATR
```bash
curl "http://localhost:8000/atr/ETHUSDT/1h?period=14"
```

### Получение EMA200
```bash
curl "http://localhost:8000/ema200/ETHUSDT/1h"
```

### Поиск лучшего уровня для лонга
```bash
curl "http://localhost:8000/best-level/ETHUSDT/long?limit=200"
```

### Поиск лучшего уровня для шорта
```bash
curl "http://localhost:8000/best-level/BTCUSDT/short?limit=200"
```

### Bulk операции
```bash
# Получение свечных данных для нескольких символов
curl "http://localhost:8000/bulk/klines?symbols=ETHUSDT,BTCUSDT&intervals=1h,4h"

# Получение нескольких индикаторов
curl "http://localhost:8000/bulk/indicators/ETHUSDT/1h?indicators=rsi,macd,bbands"

# Получение уровней S/R для нескольких символов
curl "http://localhost:8000/bulk/levels?symbols=ETHUSDT,BTCUSDT&intervals=1h,4h"
```

### Поиск уровней для лонга
```bash
curl -X POST "http://localhost:8000/levels/search" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "ETHUSDT",
    "context": "long",
    "origin_tf": "30m"
  }'
```

### Поиск уровней для шорта
```bash
curl -X POST "http://localhost:8000/levels/search" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "BTCUSDT",
    "context": "short",
    "origin_tf": "60m"
  }'
```

### Запись изменения шариков
```bash
curl -X POST "http://localhost:8000/ball-flip" \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "ETHUSDT",
    "timeframe": "30m",
    "old_color": "red",
    "new_color": "green",
    "balls": {
      "MA": "green",
      "RSI": "green",
      "Volume": "green"
    },
    "ts": "2025-01-07T12:00:00Z"
  }'
```

### Получение уровней поддержки и сопротивления
```bash
curl "http://localhost:8000/levels/AVAXUSDT/1h?limit=200"
```

### Управление кэшем
```bash
# Получение статистики кэша
curl "http://localhost:8000/cache/stats"

# Получение списка ключей
curl "http://localhost:8000/cache/keys"

# Очистка кэша
curl -X POST "http://localhost:8000/cache/clear"

# Удаление конкретного ключа
curl -X DELETE "http://localhost:8000/cache/BTCUSDT_1h_klines_limit_100"

# Обновление TTL кэша
curl -X PUT "http://localhost:8000/cache/ttl?new_ttl=120"
```

## Структура проекта

```
intraday-levels-taapi/
├── app/
│   ├── __init__.py              # makes 'app' a package
│   ├── main.py                  # Основное приложение FastAPI
│   ├── models.py                # Pydantic модели
│   ├── config.py                # Централизованная конфигурация
│   └── services/
│       ├── __init__.py          # makes 'services' a package
│       ├── cache.py             # TTLCache реализация
│       ├── taapi_service.py     # Основной сервис для работы с Taapi.io
│       ├── taapi_bulk.py        # Bulk API сервис с полным анализом уровней
│       └── level_finder.py      # Поиск лучших уровней для входа
├── requirements.txt             # Зависимости Python
├── env.example                  # Пример переменных окружения
└── README.md                   # Документация
```

## Особенности

### Кэширование
- Автоматическое кэширование результатов API запросов
- Настраиваемое время жизни кэша (TTL)
- Автоматическая очистка истекших записей
- Уменьшение нагрузки на Taapi.io API
- Детальная статистика и управление кэшем

### Bulk API
- Эффективные batch запросы к Taapi.io
- Автоматическое разбиение на батчи
- Оптимизация производительности
- Снижение количества HTTP запросов

### VWAP Интеграция
- Автоматический расчет VWAP для всех уровней
- Включение VWAP в результаты поиска уровней
- Отдельный эндпоинт для получения VWAP
- Кэширование VWAP расчетов

### Pivot Points Интеграция
- Автоматический расчет пивотных точек для всех уровней
- Включение пивотных точек в результаты поиска уровней
- Отдельный эндпоинт для получения пивотных точек
- Кэширование расчетов пивотных точек
- Расчет на основе предыдущей свечи (High, Low, Close)

### Swing Points Интеграция
- Автоматический поиск свинг-точек для всех уровней
- Включение свинг-точек в результаты поиска уровней
- Отдельный эндпоинт для получения свинг-точек
- Кэширование расчетов свинг-точек
- Кластеризация уровней по толерантности
- Настраиваемые параметры (left, right, tolerance)

### Анализ конвергенции уровней
- Автоматический анализ конвергенции факторов
- Расчет толерантности на основе ATR, цены и тика
- Оценка силы уровней по взвешенной системе
- Включение ATR и EMA200 в анализ
- Детальная информация о факторах конвергенции

### Поиск лучших уровней
- **Множественные таймфреймы** - анализ 5m, 15m, 30m, 1h, 4h
- **Динамическая толерантность** - на основе ATR, цены и тика
- **Конвергенция факторов** - VWAP, пивоты, EMA200, круглые числа
- **Соотношение риск/прибыль** - автоматический расчет RR
- **Минимальная оценка** - только уровни с оценкой >= 0.72
- **Анализ сессии** - PDH/PDL, дневные пивоты, VWAP сессии
- **Кластеризация уровней** - группировка близких уровней
- **Оценка силы** - взвешенная система оценки факторов

### Обработка ошибок
- Автоматические повторные попытки при сбоях
- Настраиваемые таймауты
- Подробное логирование ошибок
- Единообразные ответы API с полем `success`

### Rate Limiting
- Встроенная обработка rate limit ошибок от Taapi.io
- Экспоненциальная задержка при повторных попытках

### Валидация данных
- Pydantic модели для валидации входящих данных
- Строгая типизация с использованием Literal типов
- Автоматическая генерация документации OpenAPI

## Технологии

- **FastAPI** - веб-фреймворк
- **httpx** - асинхронные HTTP запросы
- **uvicorn[standard]** - ASGI сервер
- **pydantic** - валидация данных
- **pandas** - обработка данных
- **numpy** - численные вычисления
- **Taapi.io** - API технического анализа
- **python-dotenv** - управление переменными окружения

## Получение Taapi.io ключа

1. Зарегистрируйтесь на [taapi.io](https://taapi.io)
2. Получите API ключ в личном кабинете
3. Добавьте ключ в файл `.env`

## Документация API

После запуска сервера документация доступна по адресу:
- Swagger UI: http://localhost:8000/docs
- ReDoc: http://localhost:8000/redoc

## Разработка

### Добавление новых индикаторов

Для добавления нового технического индикатора:

1. Добавьте метод в `TaapiService`
2. Создайте эндпоинт в `main.py`
3. Добавьте модель в `models.py` (если нужно)
4. Обновите документацию

### Добавление новых моделей

1. Создайте класс в `models.py`
2. Используйте строгую типизацию с `Literal` для ограниченных значений
3. Добавьте валидацию при необходимости

### Работа с кэшем

```python
# Получение статистики
stats = taapi_service.get_cache_stats()

# Удаление ключа
taapi_service.delete_cache_key("BTCUSDT_1h_klines")

# Обновление TTL
taapi_service.update_cache_ttl(120)
```

### Bulk операции

```python
# Получение свечных данных для нескольких символов
klines = await taapi_service.get_klines_bulk(["ETHUSDT", "BTCUSDT"], ["1h", "4h"])

# Получение нескольких индикаторов
indicators = await taapi_service.get_indicators_bulk("ETHUSDT", "1h", ["rsi", "macd"])

# Получение уровней S/R для нескольких символов
levels = await taapi_service.get_support_resistance_bulk(["ETHUSDT", "BTCUSDT"], ["1h", "4h"])
```

### VWAP расчеты

```python
# Получение VWAP для символа
vwap_data = await taapi_service.get_vwap("ETHUSDT", "1h", 100)

# VWAP автоматически включается в уровни S/R
levels_data = await taapi_service.get_support_resistance("ETHUSDT", "1h", 100)
vwap_value = levels_data.get("vwap")
```

### Pivot Points расчеты

```python
# Получение пивотных точек для символа
pivot_data = await taapi_service.get_pivot_points("ETHUSDT", "1h", 100)

# Пивотные точки автоматически включаются в уровни S/R
levels_data = await taapi_service.get_support_resistance("ETHUSDT", "1h", 100)
pivot_points = levels_data.get("pivot_points")

# Структура пивотных точек
# {
#   "PP": 3815.2,    # Pivot Point
#   "R1": 3850.1,    # Resistance 1
#   "S1": 3780.3,    # Support 1
#   "R2": 3885.0,    # Resistance 2
#   "S2": 3745.4     # Support 2
# }
```

### Swing Points расчеты

```python
# Получение свинг-точек для символа
swing_data = await taapi_service.get_swing_points("ETHUSDT", "1h", 100, left=2, right=2, tolerance=0.001)

# Свинг-точки автоматически включаются в уровни S/R
levels_data = await taapi_service.get_support_resistance("ETHUSDT", "1h", 100)
swing_points = levels_data.get("swing_points")

# Структура свинг-точек
# {
#   "swing_highs": [(index, price), ...],      # Локальные максимумы
#   "swing_lows": [(index, price), ...],       # Локальные минимумы
#   "resistance_clusters": [price1, price2, ...], # Кластеризованные уровни сопротивления
#   "support_clusters": [price1, price2, ...]     # Кластеризованные уровни поддержки
# }
```

### Анализ конвергенции

```python
# Получение ATR для расчета толерантности
atr_data = await taapi_service.get_atr("ETHUSDT", "1h", 14)

# Получение EMA200 для анализа конвергенции
ema200_data = await taapi_service.get_ema200("ETHUSDT", "1h")

# Получение уровней с анализом конвергенции
levels_data = await taapi_service.get_support_resistance("ETHUSDT", "1h", 100)

# Структура уровня с конвергенцией
# {
#   "price": 3815.2,
#   "index": 45,
#   "strength": 3,
#   "confluence": {
#     "confluence": ["vwap", "pivot", "touches"],
#     "confluence_count": 3,
#     "factors": {
#       "vwap_near": true,
#       "pivot_near": true,
#       "ema200_near": false,
#       "round_number": false,
#       "touches": 3,
#       "trend_ok": true
#     }
#   },
#   "tolerance": 3.815,
#   "score": 0.52
# }
```

### Поиск лучших уровней

```python
# Поиск лучшего уровня для лонга
best_long = await find_levels_for_side("ETHUSDT", "long", taapi_service, 200)

# Поиск лучшего уровня для шорта
best_short = await find_levels_for_side("BTCUSDT", "short", taapi_service, 200)

# Структура лучшего уровня
# {
#   "price": 3815.2,
#   "score": 0.85,
#   "confluence": ["vwap", "pivot", "htf_swing", "touches", "trend_ok"],
#   "rr": 2.5,
#   "tolerance": 3.815,
#   "tick_size": 0.1,
#   "symbol": "ETHUSDT",
#   "side": "long",
#   "current_price": 3821.5,
#   "session_info": {
#     "vwap_session": 3815.2,
#     "pivots_daily": {...},
#     "PDH": 3850.1,
#     "PDL": 3780.3
#   },
#   "indicators": {
#     "atr": 19.05,
#     "ema200": 3780.3
#   }
# }
```