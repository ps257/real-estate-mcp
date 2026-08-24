# Danh Sách & Mô Tả Chi Tiết Công Việc Cần Làm (Task Specification)

> **Dự án**: Real Estate MCP Server  
> **Kiến trúc cơ sở**: FastMCP (Python) kết nối Supabase Postgres Database, cung cấp công cụ (MCP Tools) cho AI Agent (LangGraph).  
> **Tài liệu tham chiếu**: `docs/PLAN.md`, `docs/TOOLS_TODO.md`, `docs/SCHEMA.md`, `README.md`.

---

## 🎯 Tổng Quan Công Việc

Dự án hiện tại đã hoàn thành **Phase 1** (Khung móng FastMCP Server, 13 MCP Tools truy vấn cơ bản trên dữ liệu thực Supabase).  
Nhiệm vụ tiếp theo là **kiểm thử, gia cố Phase 1**, xây dựng **Phase 2 (RAG Policy/FAQ, Booking Persistence, DB Optimization)** và chuẩn bị cho **Phase 3 (Agent Integration & Deployment)**.

---

## 📋 Chi Tiết Các Hạng Mục Công Việc Cần Thực Hiện

### Giai Đoạn 1: Kiểm Thử, Nâng Cấp & Gia Cố Phase 1 (Verification & Hardening)

#### 1.1. Tối ưu hóa Tìm kiếm Dự án (Fuzzy Search & Vietnamese Unaccent)
- [ ] **Mô tả**: Hiện tại tool `search_projects` đang dùng `ilike` để tìm tên dự án. Cần chuyển sang sử dụng Postgres extension `pg_trgm` + `unaccent` để hỗ trợ tìm kiếm mờ (fuzzy matching) và không phụ thuộc vào dấu tiếng Việt (ví dụ: gõ "vinhome" hoặc "vinhomes" hay "Vinhomes" đều tìm ra đúng dự án).
- [ ] **Vị trí code**: `src/app/services/locations.py` -> hàm `search_projects`.
- [ ] **Tiêu chuẩn nghiệm thu**:
  - Tìm kiếm với từ khóa không dấu ("hai ba trung", "amber riverside") trả về đúng kết quả.
  - Tìm kiếm có lỗi chính tả nhẹ ("vinhome") vẫn gợi ý ra kết quả phù hợp dựa trên độ tương đồng (trgm similarity).

#### 1.2. Chạy Kiểm Thử Live DB & Đảm Bảo Test Coverage
- [ ] **Mô tả**: Chạy toàn bộ bộ test có sẵn và bổ sung test case nếu cần để xác nhận tính ổn định của 13 tool Phase 1 với live DB Supabase.
- [ ] **Các lệnh test**:
  - Unit test (không dùng DB): `pytest tests/test_shaping.py tests/test_server_tools.py -v`
  - Integration test (dùng live DB từ `.env`): `pytest tests/test_live_db.py -v`
- [ ] **Tiêu chuẩn nghiệm thu**: 100% test case pass, không có lỗi rò rỉ secret/DSN hay unhandled exception.

#### 1.3. Chuẩn Hóa Ép Kiểu Dữ Liệu & Khắc Phục Lỗi Encoded Status
- [ ] **Mô tả**: Cột dữ liệu số trong `listing` (`area_m2`, `bedrooms`, `bathrooms`, `floor_num`) hiện đang lưu dưới dạng `text`. Đảm bảo các hàm trong `src/app/shaping.py` xử lý triệt để việc ép kiểu (`to_float`, `to_int`) và chuẩn hóa trạng thái `status` (loại bỏ giá trị mojibake/corrupted).
- [ ] **Vị trí code**: `src/app/shaping.py`, `src/app/services/listings.py`.

---

### Giai Đoạn 2: Xây Dựng Các Tính Năng Phase 2 (Core Features & Database Upgrades)

#### 2.1. Xây Dựng Hệ Thống RAG Hỏi Đáp Chính Sách & Pháp Lý Dự Án (`US3` - `answer_project_policy`)
Đây là tính năng lớn nhất và quan trọng nhất trong Phase 2, nhằm phục vụ yêu cầu tỷ lệ ảo giác (hallucination) < 1% của PRD.

- [ ] **Bước 2.1.1: Database Migration cho Vector Search & Document Store**
  - Cài đặt extension `vector` (`pgvector 0.8`) trên Supabase Postgres.
  - Tạo bảng `documents`:
    ```sql
    CREATE TABLE public.documents (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id TEXT REFERENCES public.locations(id),
        doc_type TEXT, -- 'policy', 'faq', 'legal', 'amenity'
        source_url TEXT,
        chunk_index INT,
        content TEXT NOT NULL,
        embedding vector(1536), -- Hoặc dim tùy theo embedding model (ví dụ text-embedding-3-small)
        created_at TIMESTAMPTZ DEFAULT now()
    );
    CREATE INDEX idx_documents_embedding ON public.documents USING hnsw (embedding vector_cosine_ops);
    ```
  - Tạo index hỗ trợ Full-text search (BM25 / `tsvector` hoặc `pgroonga`).

- [ ] **Bước 2.1.2: Ingestion Pipeline (Nạp & Chunking Tài Liệu)**
  - Tải tài liệu chính sách, FAQ, pháp lý, tiện ích của từng dự án Vinhomes/bất động sản.
  - Xây dựng script/job thực hiện: Chia nhỏ tài liệu (chunking) -> Tạo Vector Embedding -> Lưu vào bảng `documents`.

- [ ] **Bước 2.1.3: Hybrid Retrieval & Reranking (Postgres RPC `hybrid_search_docs`)**
  - Viết Stored Procedure / Postgres RPC `hybrid_search_docs` kết hợp Full-Text Search + Vector Search bằng thuật toán **RRF (Reciprocal Rank Fusion)**.
  - Áp dụng Re-ranking (Cross-encoder) để sắp xếp lại các văn bản có độ liên quan cao nhất.

- [ ] **Bước 2.1.4: Xây Dựng Guardrail Refusal & Hoàn Thiện Tool `answer_project_policy`**
  - Vị trí code: `src/app/tools/rag.py`.
  - Cài đặt ngưỡng điểm tương đồng (Similarity Threshold Score).
  - Nếu score tìm kiếm cao hơn threshold: Trả về câu trả lời kèm `sources` và `confident=true`.
  - **Bắt buộc**: Nếu score thấp hơn threshold: Trả về `confident=false` kèm câu từ chối chuẩn mực + gợi ý kết nối với Chuyên viên tư vấn con người (đảm bảo không bịa đặt thông tin).
  - Kích hoạt tool bằng cách xóa dòng `mcp.disable(...)` trong `src/app/tools/rag.py`.

#### 2.2. Lưu Trữ Lịch Đặt Xem Nhà & Tư Vấn (Booking Persistence)
Nâng cấp `US2.1` (`start_visit_booking`) và `US2.2` (`start_consultation`) từ dạng trả về Form Spec sang dạng ghi trực tiếp vào Cơ sở dữ liệu.

- [ ] **Bước 2.2.1: Tạo Bảng `bookings` trong DB**
  - Schema đề xuất:
    ```sql
    CREATE TABLE public.bookings (
        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        project_id TEXT NOT NULL,
        kind TEXT NOT NULL, -- 'site_visit' hoặc 'consultation'
        contact JSONB NOT NULL, -- {name, phone, email}
        preferred_time TIMESTAMPTZ,
        note TEXT,
        created_at TIMESTAMPTZ DEFAULT now()
    );
    ```
- [ ] **Bước 2.2.2: Phát Triển Tool `submit_booking`**
  - Xây dựng tool `submit_booking(kind, project_id, payload)` trong `src/app/tools/cta.py`.
  - Kiểm tra hợp lệ dữ liệu đầu vào (validate phone, email, time).
  - Ghi bản ghi vào bảng `bookings` và trả về `booking_id` xác nhận.
  - Cấu hình RLS (Row Level Security) và Rate-limiting cho ghi nhận dữ liệu.

#### 2.3. Tối Ưu Bảng `listing` & Nâng Cấp Chất Lượng Tìm Kiếm
- [ ] **Chuẩn hóa Cột Số trong DB**: Tạo Generated Columns hoặc Postgres Cleaned View cho bảng `listing` để các cột `bedrooms`, `area_m2` có kiểu dữ liệu số thực sự (`INTEGER`/`NUMERIC`). Giúp lọc khoảng trực tiếp bằng SQL thay vì truy vấn toàn bộ về Python để post-filter.
- [ ] **Xây dựng Tool `search_listings_by_province`**: Bổ sung tính năng tìm kiếm bất động sản theo Tỉnh/Thành phố. Do bảng `listing` không có cột `province`, logic service cần tra cứu `locations` (level='project') theo `province` trước, lấy danh sách `project_id`, sau đó truy vấn `listing`.
- [ ] **Chuyển Đổi `project_overview` sang Postgres RPC**: Viết Postgres RPC tính toán aggregation (count, min/max/avg price, price_per_m2, bedroom mix) trực tiếp trên Postgres server để nâng cao hiệu năng.

#### 2.4. (Nâng Cao / Mở Rộng) Geo-Search & FastMCP Resource
- [ ] **Tool `nearby_listings`**: Xây dựng tool tìm kiếm bất động sản xung quanh tọa độ (`lat`, `lng`) trong bán kính `radius_m` sử dụng extension `earthdistance` hoặc `postgis`.
- [ ] **MCP Resource `realestate://project/{id}`**: Expose thông tin tổng quan của dự án dưới dạng đọc (Read-only Resource) thông qua decorator `@mcp.resource`.

---

### Giai Đoạn 3: Tích Hợp Agent, Deploy & Đánh Giá Chất Lượng (Phase 3 Integration)

- [ ] **Cấu Hình HTTP Transport Cho FastMCP**: Hỗ trợ chạy server theo giao thức HTTP Server (`MCP_TRANSPORT=http`) để LangGraph Agent kết nối tới qua endpoint `http://<host>:<port>/mcp`.
- [ ] **Tương Thích Luồng Agent**: Đảm bảo đầu ra của các tool trả về đúng định dạng payload UI Actions (CTA Buttons, Form Specs, Map Points) để LangGraph Supervisor và Frontend dễ dàng hiển thị.
- [ ] **Đánh Giá Chất Lượng (Golden Dataset Evaluation)**: Xây dựng bộ test đánh giá chất lượng RAG & Tool Calling sử dụng RAGAS / DeepEval để đo lường độ chính xác phân loại Intent, Entity Resolution và tỷ lệ ảo giác trước khi release.

---

## 📌 Bảng Tóm Tắt Trạng Thái Công Việc (Task Summary Checklist)

| STT | Công việc / Tính năng | Hạng mục | Trạng thái hiện tại | Công việc cần triển khai tiếp |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `search_projects` Fuzzy Match | Phase 1 Hardening | Đang dùng `ilike` | Nâng cấp dùng `pg_trgm` + `unaccent` |
| 2 | Verification & Testing | Phase 1 Hardening | Có sẵn khung test | Chạy test suite & đảm bảo 100% pass với live DB |
| 3 | Data Coercion & Shaping | Phase 1 Hardening | Đã cài trong `shaping.py` | Kiểm tra viền góc & kiểm thử các mẫu dữ liệu lỗi |
| 4 | RAG Database & Migration | Phase 2 Core | Chưa thực hiện | Cài `pgvector`, tạo bảng `documents` + HNSW index |
| 5 | RAG Ingestion Pipeline | Phase 2 Core | Chưa thực hiện | Viết script/job chunking & embedding tài liệu |
| 6 | Hybrid Search & Refusal | Phase 2 Core | Tool `answer_project_policy` đang disabled | Viết RPC hybrid search, cài Similarity threshold refusal, enable tool |
| 7 | Booking Persistence | Phase 2 Core | Chỉ mới có Form Spec | Tạo bảng `bookings`, viết tool `submit_booking` |
| 8 | SQL Range Filtering | Phase 2 Search Opt | Lọc `bedrooms` bằng Python | Tạo DB View / Generated Columns để lọc bằng SQL |
| 9 | `search_listings_by_province` | Phase 2 Search Opt | Chưa có | Viết service tra cứu province -> project_id -> listing |
| 10 | `project_overview` RPC | Phase 2 Perf Opt | Lấy full rows về Python | Viết Postgres Stored Procedure tính aggregation |
| 11 | HTTP Transport Deployment | Phase 3 Deploy | Đã hỗ trợ qua flag env | Chạy & kiểm thử kết nối HTTP MCP Client |
