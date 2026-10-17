-- ZXpress - MySQL/MariaDB schema (same structure as the original booking_system).
-- Idempotent: used by the Database page when copying Supabase -> XAMPP.

CREATE TABLE IF NOT EXISTS `app_users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `username` varchar(100) NOT NULL,
  `password` varchar(255) NOT NULL,
  `role` enum('admin','user') NOT NULL DEFAULT 'user',
  `created_at` timestamp NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`),
  UNIQUE KEY `username` (`username`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `bookings` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `code` varchar(20) DEFAULT NULL,
  `code_fullform` varchar(255) DEFAULT NULL,
  `booking_date` date DEFAULT NULL,
  `awb_no` varchar(50) DEFAULT NULL,
  `destination` varchar(100) DEFAULT NULL,
  `weight` decimal(10,3) DEFAULT NULL,
  `courier` varchar(100) DEFAULT NULL,
  `zone` varchar(100) DEFAULT NULL,
  `auto_amount` decimal(12,2) DEFAULT NULL,
  `fuel` decimal(12,2) DEFAULT NULL,
  `total_amount` decimal(12,2) DEFAULT NULL,
  `gst_18pct` decimal(12,2) DEFAULT NULL,
  `net_total` decimal(12,2) DEFAULT NULL,
  `month` varchar(20) DEFAULT NULL,
  `client_name` varchar(255) DEFAULT NULL,
  `inv_no` varchar(50) DEFAULT NULL,
  `inv_date` date DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `day_wise` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `entry_date` date DEFAULT NULL,
  `total_weight` decimal(10,3) DEFAULT NULL,
  `total_sales` decimal(10,2) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `license` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `license_key` varchar(100) NOT NULL,
  `expiry_date` date NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT current_timestamp(),
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `place_zones` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `place` varchar(100) NOT NULL,
  `zone` tinyint(4) NOT NULL,
  `place_code` varchar(20) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `place` (`place`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `rates` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `code` varchar(10) NOT NULL,
  `code_fullform` varchar(200) DEFAULT NULL,
  `place` varchar(100) DEFAULT NULL,
  `zone` tinyint(4) DEFAULT 5,
  `rate_250g` decimal(10,2) DEFAULT NULL,
  `rate_500g` decimal(10,2) DEFAULT NULL,
  `rate_500g_1` decimal(10,2) DEFAULT NULL,
  `rate_1_to_3kg` decimal(10,2) DEFAULT NULL,
  `rate_3_to_10kg` decimal(10,2) DEFAULT NULL,
  `rate_above_10kg` decimal(10,2) DEFAULT NULL,
  `fuel` decimal(10,2) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `users` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `username` varchar(100) NOT NULL,
  `password` varchar(100) NOT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;

CREATE TABLE IF NOT EXISTS `zones` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `district` varchar(100) DEFAULT NULL,
  `rate_zone` varchar(50) DEFAULT NULL,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci;
