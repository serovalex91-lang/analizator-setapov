from dataclasses import dataclass


@dataclass
class Settings:
	# Доля свечи (0..1), которую цена должна пройти за пределы границы зоны
	# для позиционного подтверждения уровня (правило 70%).
	position_threshold: float = 0.70
	# Ширина зоны уровня относительно ATR (в исходнике ± ATR/2)
	zone_half_atr: float = 0.5
	# Окно последних свечей для оценки атаки
	attack_window: int = 5




