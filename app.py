from flask import Flask, render_template, request, jsonify, send_from_directory
import pandas as pd
import datetime
import math
import requests
import json
import os
import io
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# === КОНСТАНТЫ И НАСТРОЙКИ ===
load_dotenv()
app = Flask(__name__)

# Конфигурация API
TOMTOM_API_KEY = os.getenv('TOMTOM_API_KEY')
MAX_POINTS = 15

# Константы для пробок
TRAFFIC_LEVELS = {
    'low': {'multiplier': 1.0, 'text': 'низкий'},
    'medium': {'multiplier': 1.3, 'text': 'средний'},
    'high': {'multiplier': 1.8, 'text': 'высокий'},
    'very_high': {'multiplier': 2.5, 'text': 'очень высокий'}
}

# Маппинг типов инцидентов
INCIDENT_TYPES = {
    'ACCIDENT': 'ДТП',
    'ROAD_CLOSED': 'Перекрытие дороги',
    'ROAD_WORKS': 'Дорожные работы',
    'WEATHER': 'Погодные условия',
    'JAM': 'Затор',
    'HAZARD': 'Препятствие'
}

# === УТИЛИТЫ ДЛЯ РАБОТЫ С ФАЙЛАМИ ===

def load_addresses():
    """Загружает адреса из CSV файла"""
    try:
        if not os.path.exists('addresses.csv'):
            return pd.DataFrame()
            
        df = pd.read_csv('addresses.csv', encoding='utf-8')
        print(f"✅ Загружено {len(df)} адресов из файла")
        return df
        
    except Exception as e:
        print(f"❌ Ошибка загрузки файла addresses.csv: {e}")
        return pd.DataFrame()

def save_addresses(df):
    """Сохраняет DataFrame в CSV файл"""
    try:
        df.to_csv('addresses.csv', index=False, encoding='utf-8')
        return True
    except Exception as e:
        print(f"❌ Ошибка сохранения файла: {e}")
        return False

def create_backup():
    """Создает резервную копию файла адресов"""
    try:
        if os.path.exists('addresses.csv'):
            timestamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_name = f"addresses_backup_{timestamp}.csv"
            os.rename('addresses.csv', backup_name)
            return backup_name
    except Exception as e:
        print(f"❌ Ошибка создания бэкапа: {e}")
    return None

# === РАБОТА С TOMTOM API ===

class TomTomService:
    """Сервис для работы с TomTom API"""
    
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.tomtom.com/traffic/services/4"
    
    def get_traffic_data(self, bbox=None):
        """Получает данные о пробках"""
        if not self.api_key:
            return self._get_fallback_data(bbox)
        
        try:
            if not bbox:
                bbox = [39.5, 47.1, 40.0, 47.4]  # Ростов-на-Дону по умолчанию
            
            # Получаем данные о потоке трафика
            flow_data = self._get_flow_data(bbox)
            # Получаем инциденты
            incidents = self._get_incidents(bbox)
            
            return self._parse_traffic_data(flow_data, incidents, bbox)
            
        except Exception as e:
            print(f"❌ Ошибка TomTom API: {e}")
            return self._get_fallback_data(bbox)
    
    def _get_flow_data(self, bbox):
        """Получает данные о потоке трафика"""
        center_lat = (bbox[1] + bbox[3]) / 2
        center_lon = (bbox[0] + bbox[2]) / 2
        
        url = f"{self.base_url}/flowSegmentData/absolute/10/json"
        params = {
            'point': f"{center_lat},{center_lon}",
            'unit': 'KMPH',
            'zoom': 12,
            'key': self.api_key
        }
        
        response = requests.get(url, params=params, timeout=10)
        return response.json() if response.status_code == 200 else {}
    
    def _get_incidents(self, bbox):
        """Получает информацию об инцидентах"""
        bbox_str = f"{bbox[1]},{bbox[0]},{bbox[3]},{bbox[2]}"
        url = f"{self.base_url}/incidentDetails"
        
        params = {
            'bbox': bbox_str,
            'fields': '{incidents{type,geometry,properties}}',
            'language': 'ru-RU',
            'key': self.api_key,
            'categoryFilter': '0,1,2,3,4,5,6,7,8,9,10,11,14'
        }
        
        response = requests.get(url, params=params, timeout=10)
        return response.json() if response.status_code == 200 else {}
    
    def _parse_traffic_data(self, flow_data, incidents_data, bbox):
        """Парсит данные от TomTom"""
        traffic_data = {
            'traffic_level': 'low',
            'incidents': [],
            'message': 'Дорожная ситуация нормальная',
            'timestamp': datetime.datetime.now().isoformat(),
            'source': 'tomtom'
        }
        
        try:
            # Анализ данных о потоке
            if 'flowSegmentData' in flow_data:
                segment = flow_data['flowSegmentData']
                current_speed = segment.get('currentSpeed', 0)
                free_flow_speed = segment.get('freeFlowSpeed', 0)
                
                if free_flow_speed > 0:
                    speed_ratio = current_speed / free_flow_speed
                    traffic_level = self._calculate_traffic_level(speed_ratio)
                    
                    traffic_data.update({
                        'traffic_level': traffic_level,
                        'message': self._get_traffic_message(traffic_level),
                        'current_speed': current_speed,
                        'free_flow_speed': free_flow_speed,
                        'congestion_ratio': round((1 - speed_ratio) * 100, 1)
                    })
            
            # Парсинг инцидентов
            traffic_data['incidents'] = self._parse_incidents(incidents_data)
            if traffic_data['incidents']:
                traffic_data['message'] += f', {len(traffic_data["incidents"])} инцидентов'
            
            print(f"✅ TomTom: уровень {traffic_data['traffic_level']}")
            return traffic_data
            
        except Exception as e:
            print(f"❌ Ошибка парсинга TomTom данных: {e}")
            return self._get_fallback_data(bbox)
    
    def _calculate_traffic_level(self, speed_ratio):
        """Рассчитывает уровень пробок на основе соотношения скоростей"""
        if speed_ratio >= 0.8: return 'low'
        elif speed_ratio >= 0.5: return 'medium'
        elif speed_ratio >= 0.3: return 'high'
        else: return 'very_high'
    
    def _get_traffic_message(self, level):
        """Возвращает текстовое описание уровня пробок"""
        messages = {
            'low': 'Свободное движение',
            'medium': 'Умеренное движение',
            'high': 'Плотное движение',
            'very_high': 'Пробки'
        }
        return messages.get(level, 'Неизвестно')
    
    def _parse_incidents(self, incidents_data):
        """Парсит данные об инцидентах"""
        incidents = []
        
        try:
            for incident in incidents_data.get('incidents', []):
                incident_type = incident.get('type', 'unknown')
                properties = incident.get('properties', {})
                
                incidents.append({
                    'type': INCIDENT_TYPES.get(incident_type, incident_type),
                    'description': properties.get('description', ''),
                    'severity': properties.get('magnitudeOfDelay', 'medium'),
                    'location': self._extract_incident_location(incident)
                })
            
            return incidents
        except Exception as e:
            print(f"❌ Ошибка парсинга инцидентов: {e}")
            return []
    
    def _extract_incident_location(self, incident):
        """Извлекает координаты инцидента"""
        try:
            geometry = incident.get('geometry', {})
            if geometry.get('type') == 'Point' and 'coordinates' in geometry:
                coords = geometry['coordinates']
                return {'lon': coords[0], 'lat': coords[1]}
        except:
            pass
        return {'lat': 47.222, 'lon': 39.715}
    
    def _get_fallback_data(self, bbox):
        """Фолбэк данные если API недоступно"""
        print("⚠️ Используем фолбэк данные о пробках")
        return self._simulate_traffic_data(bbox)
    
    def _simulate_traffic_data(self, bbox):
        """Симуляция данных о пробках"""
        import random
        
        current_hour = datetime.datetime.now().hour
        
        # Логика определения уровня пробок по времени суток
        if 7 <= current_hour <= 10 or 17 <= current_hour <= 20:
            level = random.choice(['high', 'very_high'])
        elif 11 <= current_hour <= 16:
            level = random.choice(['medium', 'high'])
        else:
            level = 'low'
        
        incidents = []
        if level in ['high', 'very_high']:
            incident_types = ['ДТП', 'Ремонт дороги', 'Перекрытие', 'Затор']
            for _ in range(random.randint(1, 3)):
                incidents.append({
                    'type': random.choice(incident_types),
                    'location': {
                        'lat': bbox[1] + (bbox[3] - bbox[1]) * random.random(),
                        'lon': bbox[0] + (bbox[2] - bbox[0]) * random.random()
                    },
                    'description': f'{random.choice(incident_types)} на участке дороги',
                    'severity': random.choice(['low', 'medium', 'high'])
                })
        
        return {
            'traffic_level': level,
            'incidents': incidents,
            'simulated': True
        }

# Инициализация сервиса TomTom
tomtom_service = TomTomService(TOMTOM_API_KEY)

# === СЕРВИС ДЛЯ РАБОТЫ С МАРШРУТАМИ ===

class RouteService:
    """Сервис для работы с маршрутами"""
    
    def __init__(self):
        self.osrm_base_url = "http://router.project-osrm.org/route/v1/driving"
    
    def get_route(self, coordinates, avoid_traffic=True):
        """Получает маршрут от OSRM"""
        if len(coordinates) < 2:
            return None
            
        try:
            coords_str = ';'.join([f"{lon},{lat}" for lat, lon in coordinates])
            url = f"{self.osrm_base_url}/{coords_str}"
            
            params = {
                'overview': 'full',
                'geometries': 'geojson',
                'steps': 'true'
            }
            
            traffic_data = {}
            if avoid_traffic:
                traffic_data = self._get_route_traffic_data(coordinates)
            
            print(f"🛣️ Запрос маршрута OSRM для {len(coordinates)} точек...")
            response = requests.get(url, params=params, timeout=15)
            
            if response.status_code == 200:
                data = response.json()
                if data['code'] == 'Ok':
                    return self._parse_route_data(data['routes'][0], coordinates, traffic_data, avoid_traffic)
            
            return None
            
        except Exception as e:
            print(f"❌ Ошибка OSRM: {e}")
            return None
    
    def _get_route_traffic_data(self, coordinates):
        """Получает данные о пробках для маршрута"""
        bbox = [
            min(lon for _, lon in coordinates) - 0.1,
            min(lat for lat, _ in coordinates) - 0.1,
            max(lon for _, lon in coordinates) + 0.1,
            max(lat for lat, _ in coordinates) + 0.1
        ]
        return tomtom_service.get_traffic_data(bbox)
    
    def _parse_route_data(self, route, coordinates, traffic_data, avoid_traffic):
        """Парсит данные маршрута"""
        route_info = {
            'distance_km': route['distance'] / 1000,
            'duration_min': route['duration'] / 60,
            'geometry': route['geometry'],
            'traffic_data': traffic_data if avoid_traffic else {},
            'waypoints': coordinates
        }
        
        # Корректируем время с учетом пробок
        if avoid_traffic and traffic_data:
            multiplier = TRAFFIC_LEVELS.get(traffic_data['traffic_level'], {}).get('multiplier', 1.0)
            route_info['duration_min'] *= multiplier
            route_info['original_duration_min'] = route['duration'] / 60
            route_info['traffic_impact'] = f"+{int((multiplier-1)*100)}%"
            
            if traffic_data.get('congestion_ratio'):
                route_info['congestion'] = f"{traffic_data['congestion_ratio']}%"
        
        print(f"✅ Маршрут OSRM: {route_info['distance_km']:.1f} км, {route_info['duration_min']:.1f} мин")
        return route_info

# Инициализация сервиса маршрутов
route_service = RouteService()

# === ОПТИМИЗАЦИЯ МАРШРУТОВ ===

class RouteOptimizer:
    """Класс для оптимизации маршрутов"""
    
    def __init__(self):
        self.current_time = datetime.datetime.now().replace(second=0, microsecond=0)
    
    def optimize_with_timing(self, selected_df, user_location=None, avoid_traffic=True):
        """Оптимизация маршрута с учетом времени"""
        if selected_df.empty:
            return selected_df, [], {}
        
        # Подготавливаем данные
        df = self._prepare_data(selected_df)
        # Сортируем маршрут
        optimal_route = self._sort_route(df)
        # Получаем маршрут
        route_info = self._get_route_info(optimal_route, user_location, avoid_traffic)
        # Создаем расписание
        schedule = self._create_schedule(optimal_route, route_info, avoid_traffic)
        
        return optimal_route, schedule, route_info
    
    def _prepare_data(self, df):
        """Подготавливает данные для оптимизации"""
        df = df.reset_index(drop=True).copy()
        df['priority'] = df['Уровень клиента'].apply(lambda x: 0 if x == 'VIP' else 1)
        df['temp_index'] = df.index
        return df
    
    def _sort_route(self, df):
        """Сортирует точки маршрута"""
        optimal_route = df.sort_values(['priority', 'temp_index'])
        return optimal_route.drop('temp_index', axis=1)
    
    def _get_route_info(self, optimal_route, user_location, avoid_traffic):
        """Получает информацию о маршруте"""
        waypoints = []
        if user_location:
            waypoints.append([user_location[0], user_location[1]])
        
        for _, point in optimal_route.iterrows():
            waypoints.append([point['Географическая широта'], point['Географическая долгота']])
        
        return route_service.get_route(waypoints, avoid_traffic) if len(waypoints) > 1 else {}
    
    def _create_schedule(self, optimal_route, route_info, avoid_traffic):
        """Создает расписание посещений"""
        schedule = []
        current_time = self.current_time
        
        for i, (_, point) in enumerate(optimal_route.iterrows()):
            # Корректируем время с учетом рабочего графика
            current_time = self._adjust_time_for_schedule(current_time, point)
            
            # Создаем запись в расписании
            schedule_entry = self._create_schedule_entry(i, point, current_time)
            schedule.append(schedule_entry)
            
            # Обновляем время для следующей точки
            current_time = self._calculate_next_time(current_time, schedule_entry, route_info, i, len(optimal_route), avoid_traffic)
        
        return schedule
    
    def _adjust_time_for_schedule(self, current_time, point):
        """Корректирует время с учетом рабочего графика точки"""
        work_start = datetime.datetime.strptime(point['Время начала рабочего дня'], '%H:%M').time()
        work_end = datetime.datetime.strptime(point['Время окончания рабочего дня'], '%H:%M').time()
        lunch_start = datetime.datetime.strptime(point['Время начала обеда'], '%H:%M').time()
        lunch_end = datetime.datetime.strptime(point['Время окончания обеда'], '%H:%M').time()
        
        # Проверяем обеденное время
        if lunch_start <= current_time.time() <= lunch_end:
            wait_until = datetime.datetime.combine(current_time.date(), lunch_end)
            if current_time < wait_until:
                current_time = wait_until
        
        # Проверяем время работы
        if current_time.time() < work_start:
            wait_until = datetime.datetime.combine(current_time.date(), work_start)
            current_time = wait_until
        
        return current_time
    
    def _create_schedule_entry(self, index, point, current_time):
        """Создает запись в расписании"""
        visit_duration = 45 if point['Уровень клиента'] == 'VIP' else 30
        duration_td = datetime.timedelta(minutes=visit_duration)
        departure_time = current_time + duration_td
        
        return {
            'order': index + 1,
            'address': point['Адрес объекта'],
            'arrival_time': current_time.strftime('%H:%M'),
            'departure_time': departure_time.strftime('%H:%M'),
            'date': current_time.strftime('%d.%m.%Y'),
            'client_type': point['Уровень клиента'],
            'duration': visit_duration,
            'work_time': f"{point['Время начала рабочего дня']}-{point['Время окончания рабочего дня']}",
            'lunch_time': f"{point['Время начала обеда']}-{point['Время окончания обеда']}"
        }
    
    def _calculate_next_time(self, current_time, schedule_entry, route_info, current_index, total_points, avoid_traffic):
        """Рассчитывает время для следующей точки"""
        departure_time = datetime.datetime.strptime(schedule_entry['departure_time'], '%H:%M')
        departure_time = departure_time.replace(year=current_time.year, month=current_time.month, day=current_time.day)
        
        if route_info and current_index < total_points - 1:
            segment_time = route_info.get('duration_min', 15) / total_points
            if avoid_traffic and route_info.get('traffic_data', {}).get('traffic_level') in ['high', 'very_high']:
                segment_time *= 1.5
            travel_time = datetime.timedelta(minutes=max(segment_time, 5))
        else:
            travel_time = datetime.timedelta(minutes=15)
        
        return departure_time + travel_time

# === ОБРАБОТЧИКИ CSV ФАЙЛОВ ===

class CSVHandler:
    """Обработчик CSV файлов"""
    
    def __init__(self):
        self.required_columns = [
            'Адрес объекта', 'Уровень клиента', 'Географическая широта', 
            'Географическая долгота', 'Время начала рабочего дня',
            'Время окончания рабочего дня', 'Время начала обеда', 'Время окончания обеда'
        ]
        
        self.column_mapping = {
            'адрес объекта': 'Адрес объекта',
            'уровень клиента': 'Уровень клиента', 
            'географическая широта': 'Географическая широта',
            'географическая долгота': 'Географическая долгота',
            'время начала рабочего дня': 'Время начала рабочего дня',
            'время окончания рабочего дня': 'Время окончания рабочего дня',
            'время начала обеда': 'Время начала обеда',
            'время окончания обеда': 'Время окончания обеда',
        }
    
    def parse_uploaded_file(self, file_content):
        """Парсит загруженный CSV файл"""
        # Пробуем разные разделители
        for delimiter in [',', ';', '\t']:
            try:
                df = pd.read_csv(io.StringIO(file_content), encoding='utf-8', delimiter=delimiter)
                if len(df.columns) > 1:
                    print(f"✅ Найден разделитель: '{delimiter}', колонок: {len(df.columns)}")
                    return self._clean_dataframe(df)
            except:
                continue
        
        raise ValueError("Не удалось определить разделитель CSV файла")
    
    def _clean_dataframe(self, df):
        """Очищает и проверяет DataFrame"""
        # Очищаем названия колонок
        df.columns = df.columns.str.strip().str.replace('"', '').str.replace("'", "")
        
        # Приводим к стандартным названиям
        new_columns = []
        for col in df.columns:
            clean_col = col.strip().lower()
            new_columns.append(self.column_mapping.get(clean_col, col))
        
        df.columns = new_columns
        
        # Проверяем обязательные колонки
        missing_columns = [col for col in self.required_columns if col not in df.columns]
        if missing_columns:
            raise ValueError(f"Отсутствуют обязательные колонки: {', '.join(missing_columns)}")
        
        # Исправляем опечатку в типах клиентов
        if 'Уровень клиента' in df.columns:
            df['Уровень клиента'] = df['Уровень клиента'].replace({'Standart': 'Standard'})
        
        print(f"✅ Проверка данных: {len(df)} строк, типы клиентов: {df['Уровень клиента'].unique()}")
        return df

# Инициализация обработчика CSV
csv_handler = CSVHandler()

# === ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ===

def prepare_address_data(df):
    """Подготавливает данные адресов для API"""
    addresses = []
    
    if df.empty:
        return addresses
    
    for idx, row in df.iterrows():
        client_type = row['Уровень клиента']
        if client_type == 'Standart':
            client_type = 'Standard'
        
        addresses.append({
            'id': idx,
            'address': row['Адрес объекта'],
            'lat': float(row['Географическая широта']),
            'lon': float(row['Географическая долгота']),
            'work_time_start': row['Время начала рабочего дня'],
            'work_time_end': row['Время окончания рабочего дня'],
            'lunch_start': row['Время начала обеда'],
            'lunch_end': row['Время окончания обеда'],
            'client_type': client_type,
            'visit_duration': 45 if client_type == 'VIP' else 30
        })
    
    return addresses

def prepare_traffic_response(traffic_data, route_info):
    """Подготавливает ответ с информацией о пробках"""
    if not traffic_data:
        return None
    
    traffic_details = []
    
    # Уровень пробок
    level_text = TRAFFIC_LEVELS.get(traffic_data['traffic_level'], {}).get('text', 'неизвестный')
    traffic_details.append(f"Уровень пробок: {level_text}")
    
    # Скорость движения
    if traffic_data.get('current_speed') and traffic_data.get('free_flow_speed'):
        current_speed = traffic_data['current_speed']
        free_flow_speed = traffic_data['free_flow_speed']
        traffic_details.append(f"Скорость: {current_speed} км/ч (свободно: {free_flow_speed} км/ч)")
    
    # Загруженность дорог
    if traffic_data.get('congestion_ratio'):
        traffic_details.append(f"Загруженность дорог: {traffic_data['congestion_ratio']}%")
    
    # Инциденты
    incidents_count = len(traffic_data.get('incidents', []))
    if incidents_count > 0:
        incident_types = {}
        for incident in traffic_data['incidents']:
            incident_type = incident['type']
            incident_types[incident_type] = incident_types.get(incident_type, 0) + 1
        
        incidents_text = [f"{incident_type}: {count}" for incident_type, count in incident_types.items()]
        traffic_details.append(f"Дорожные инциденты: {', '.join(incidents_text)}")
    else:
        traffic_details.append("Дорожные инциденты: не обнаружены")
    
    # Влияние на маршрут
    if route_info.get('traffic_impact'):
        traffic_details.append(f"Влияние на время: {route_info['traffic_impact']}")
    
    # Источник данных
    source_text = "реальные данные TomTom" if traffic_data.get('source') == 'tomtom' else "симулированные данные"
    traffic_details.append(f"Источник: {source_text}")
    
    return {
        'level': traffic_data['traffic_level'],
        'message': traffic_data['message'],
        'incidents_count': incidents_count,
        'traffic_impact': route_info.get('traffic_impact'),
        'congestion': route_info.get('congestion'),
        'source': traffic_data.get('source', 'simulated'),
        'details': traffic_details,
        'has_traffic': traffic_data['traffic_level'] in ['medium', 'high', 'very_high'],
        'has_incidents': incidents_count > 0
    }

# === FLASK ROUTES ===

@app.route('/')
def index():
    """Главная страница"""
    df = load_addresses()
    addresses_count = len(df) if not df.empty else 0
    print(f"📊 Передано в шаблон: {addresses_count} адресов")
    return render_template('index.html', addresses_count=addresses_count)

@app.route('/get_addresses')
def get_addresses():
    """API для загрузки адресов"""
    df = load_addresses()
    
    if df.empty:
        return jsonify({'error': 'Файл addresses.csv не найден или пуст'}), 404
    
    addresses = prepare_address_data(df)
    print(f"📨 Отправлено {len(addresses)} адресов через API")
    return jsonify(addresses)

@app.route('/addresses.csv')
def serve_addresses_csv():
    """Отдает файл addresses.csv"""
    try:
        return send_from_directory(os.path.dirname(os.path.abspath(__file__)), 'addresses.csv')
    except Exception as e:
        print(f"❌ Ошибка при обслуживании файла: {e}")
        return "File not found", 404

@app.route('/optimize', methods=['POST'])
def optimize():
    """Оптимизация маршрута"""
    try:
        data = request.json
        selected_indices = data.get('points', [])
        user_location = data.get('user_location')
        avoid_traffic = data.get('avoid_traffic', True)
        
        if not selected_indices:
            return jsonify({'success': False, 'error': 'Не выбраны точки'})
        
        print(f"🚗 Оптимизация маршрута для {len(selected_indices)} точек, пробки: {'включены' if avoid_traffic else 'выключены'}")
        
        df = load_addresses()
        if df.empty:
            return jsonify({'success': False, 'error': 'Файл с адресами не загружен'})
        
        # Проверяем индексы
        df = df.reset_index(drop=True)
        valid_indices = [idx for idx in selected_indices if idx < len(df)]
        if not valid_indices:
            return jsonify({'success': False, 'error': 'Некорректные индексы точек'})
        
        # Подготавливаем данные
        selected_df = df.iloc[valid_indices].copy()
        selected_df['Уровень клиента'] = selected_df['Уровень клиента'].replace({'Standart': 'Standard'})
        
        # Оптимизируем маршрут
        optimizer = RouteOptimizer()
        optimal_route, schedule, route_info = optimizer.optimize_with_timing(
            selected_df, user_location, avoid_traffic
        )
        
        # Подготавливаем ответ
        vip_count = len(optimal_route[optimal_route['Уровень клиента'] == 'VIP'])
        standard_count = len(optimal_route[optimal_route['Уровень клиента'] == 'Standard'])
        
        map_data = {
            'user_location': user_location,
            'points': [],
            'route_info': route_info,
            'traffic_data': route_info.get('traffic_data', {}) if avoid_traffic else {}
        }
        
        # Добавляем точки маршрута
        for i, (_, point) in enumerate(optimal_route.iterrows()):
            map_data['points'].append({
                'order': i + 1,
                'lat': float(point['Географическая широта']),
                'lon': float(point['Географическая долгота']),
                'address': point['Адрес объекта'],
                'type': 'VIP' if point['Уровень клиента'] == 'VIP' else 'Standard',
                'work_time': f"{point['Время начала рабочего дня']}-{point['Время окончания рабочего дня']}",
                'lunch_time': f"{point['Время начала обеда']}-{point['Время окончания обеда']}"
            })
        
        response_data = {
            'success': True,
            'map_data': map_data,
            'schedule': schedule,
            'total_points': len(optimal_route),
            'total_distance': round(route_info.get('distance_km', 0), 2),
            'total_time': round(route_info.get('duration_min', 0) / 60, 1),
            'route_duration_min': round(route_info.get('duration_min', 0), 1),
            'vip_count': vip_count,
            'standard_count': standard_count,
            'avoid_traffic': avoid_traffic
        }
        
        # Добавляем информацию о пробках
        if avoid_traffic and route_info.get('traffic_data'):
            response_data['traffic_info'] = prepare_traffic_response(
                route_info['traffic_data'], route_info
            )
        
        return jsonify(response_data)
        
    except Exception as e:
        print(f"❌ Ошибка при оптимизации: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/get_traffic_info')
def get_traffic_info():
    """Информация о дорожной ситуации"""
    try:
        bbox_param = request.args.get('bbox')
        bbox = [float(coord) for coord in bbox_param.split(',')] if bbox_param else None
        
        traffic_data = tomtom_service.get_traffic_data(bbox)
        return jsonify(traffic_data)
        
    except Exception as e:
        return jsonify({
            'traffic_level': 'unknown',
            'incidents': [],
            'message': f'Ошибка: {str(e)}',
            'timestamp': datetime.datetime.now().isoformat(),
            'source': 'error'
        })

@app.route('/test_tomtom')
def test_tomtom():
    """Тестовый endpoint для TomTom API"""
    try:
        bbox = [39.5, 47.1, 40.0, 47.4]
        traffic_data = tomtom_service.get_traffic_data(bbox)
        
        return jsonify({
            'tomtom_api_key_exists': bool(TOMTOM_API_KEY),
            'tomtom_response': traffic_data,
            'status': 'success'
        })
    except Exception as e:
        return jsonify({
            'tomtom_api_key_exists': bool(TOMTOM_API_KEY),
            'error': str(e),
            'status': 'error'
        })

@app.route('/upload_addresses', methods=['POST'])
def upload_addresses():
    """Загрузка нового CSV файла"""
    try:
        if 'csv_file' not in request.files:
            return jsonify({'success': False, 'error': 'Файл не выбран'})
        
        file = request.files['csv_file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'Файл не выбран'})
        
        if not file.filename.endswith('.csv'):
            return jsonify({'success': False, 'error': 'Файл должен быть в формате CSV'})
        
        # Читаем и парсим файл
        file_content = file.read().decode('utf-8')
        new_df = csv_handler.parse_uploaded_file(file_content)
        
        # Создаем бэкап и сохраняем
        backup_name = create_backup()
        save_addresses(new_df)
        
        # Загружаем обновленные данные
        updated_df = load_addresses()
        
        response_data = {
            'success': True,
            'message': f'Успешно загружено {len(new_df)} адресов',
            'total_addresses': len(updated_df),
            'backup_created': backup_name is not None,
            'backup_name': backup_name
        }
        
        print(f"✅ Загружен новый файл с {len(new_df)} адресами")
        return jsonify(response_data)
        
    except Exception as e:
        print(f"❌ Ошибка загрузки файла: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/add_single_address', methods=['POST'])
def add_single_address():
    """Добавление одной точки"""
    try:
        data = request.json
        
        # Проверяем обязательные поля
        required_fields = ['address', 'client_type', 'lat', 'lon']
        missing_fields = [field for field in required_fields if field not in data]
        if missing_fields:
            return jsonify({'success': False, 'error': f'Отсутствуют поля: {", ".join(missing_fields)}'})
        
        # Загружаем текущие адреса
        df = load_addresses()
        
        # Создаем новую запись
        new_address = {
            'Адрес объекта': data['address'],
            'Уровень клиента': data['client_type'],
            'Географическая широта': float(data['lat']),
            'Географическая долгота': float(data['lon']),
            'Время начала рабочего дня': data.get('work_time_start', '09:00'),
            'Время окончания рабочего дня': data.get('work_time_end', '18:00'),
            'Время начала обеда': data.get('lunch_start', '13:00'),
            'Время окончания обеда': data.get('lunch_end', '14:00')
        }
        
        # Добавляем и сохраняем
        new_df = pd.concat([df, pd.DataFrame([new_address])], ignore_index=True)
        save_addresses(new_df)
        
        updated_df = load_addresses()
        
        return jsonify({
            'success': True,
            'message': 'Адрес успешно добавлен',
            'total_addresses': len(updated_df)
        })
        
    except Exception as e:
        print(f"❌ Ошибка добавления адреса: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/delete_address', methods=['POST'])
def delete_address():
    """Удаление одной точки"""
    try:
        data = request.json
        address_id = data.get('address_id')
        
        if address_id is None:
            return jsonify({'success': False, 'error': 'ID адреса не указан'})
        
        df = load_addresses()
        if df.empty:
            return jsonify({'success': False, 'error': 'Файл с адресами не найден'})
        
        df = df.reset_index(drop=True)
        
        if address_id >= len(df) or address_id < 0:
            return jsonify({'success': False, 'error': 'Адрес с указанным ID не найден'})
        
        # Удаляем и сохраняем
        df = df.drop(address_id).reset_index(drop=True)
        save_addresses(df)
        
        updated_df = load_addresses()
        
        return jsonify({
            'success': True,
            'message': 'Адрес успешно удален',
            'total_addresses': len(updated_df)
        })
        
    except Exception as e:
        print(f"❌ Ошибка удаления адреса: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/delete_all_addresses', methods=['POST'])
def delete_all_addresses():
    """Удаление всех точек"""
    try:
        df = load_addresses()
        address_count = len(df)
        
        if address_count == 0:
            return jsonify({'success': False, 'error': 'Нет адресов для удаления'})
        
        # Создаем пустой DataFrame и сохраняем
        empty_df = pd.DataFrame(columns=csv_handler.required_columns)
        save_addresses(empty_df)
        
        # Создаем бэкап
        backup_name = f"addresses_deleted_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        df.to_csv(backup_name, index=False, encoding='utf-8')
        
        print(f"🗑️ Удалено всех адресов: {address_count}, создан бэкап: {backup_name}")
        
        return jsonify({
            'success': True,
            'message': f'Все адресы ({address_count}) успешно удалены',
            'total_addresses': 0,
            'backup_created': backup_name
        })
        
    except Exception as e:
        print(f"❌ Ошибка удаления всех адресов: {e}")
        return jsonify({'success': False, 'error': str(e)})

# === ЗАПУСК ПРИЛОЖЕНИЯ ===

if __name__ == '__main__':
    print("🚗 Запуск навигатора с Leaflet картами...")
    print("✅ Используется бесплатный OSRM для маршрутизации")
    print("📁 Данные загружаются ТОЛЬКО из addresses.csv")
    
    if TOMTOM_API_KEY:
        print("🎯 TomTom API подключен для данных о пробках")
    else:
        print("⚠️ TomTom API ключ не найден, используем симуляцию пробок")
    
    # Проверяем файл при запуске
    df = load_addresses()
    if df.empty:
        print("❌ ВНИМАНИЕ: Файл addresses.csv не найден или пуст!")
        print("📝 Создайте файл addresses.csv со следующими колонками:")
        print("   Адрес объекта, Уровень клиента, Географическая широта, Географическая долгота, Время начала рабочего дня, Время окончания рабочего дня, Время начала обеда, Время окончания обеда")
    else:
        vip_count = len(df[df['Уровень клиента'] == 'VIP'])
        standard_count = len(df[df['Уровень клиента'] == 'Standard'])
        print(f"✅ Загружено {len(df)} адресов")
        print(f"📊 Статистика: VIP: {vip_count}, Standard: {standard_count}")
    
    app.run(debug=True, host='0.0.0.0', port=5000)