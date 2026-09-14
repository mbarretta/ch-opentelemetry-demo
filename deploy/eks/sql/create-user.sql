-- create the user: clickstack with grants on the otel database
-- The clickstack user will be used by the ClickHouse exporter to write data to the otel database
CREATE USER clickstack IDENTIFIED WITH sha256_password BY 'SECURE_PASSWORD';
GRANT CREATE DATABASE ON otel.* to clickstack;
GRANT SELECT, INSERT, CREATE TABLE ON otel.* TO clickstack;