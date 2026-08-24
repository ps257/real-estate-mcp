# Tổng Quan Các MCP Tools, User Stories (US) & Phân Tích Miền Nghiệp Vụ (Domain Analysis)

> **Tài liệu cho dán nhãn & định hướng phát triển**: Báo cáo phân loại các MCP Tools theo User Story, Miền Nghiệp vụ (Domain), và chỉ rõ các tool liên quan đến **Phân Tích Tổng Quan (Market Overview & Analytics)**.

---

## 🎯 1. Danh Sách Chi Tiết Các Tool, User Story & Miền Nghiệp Vụ (Domain)

Bảng dưới đây phân loại toàn bộ 14 MCP Tools trong dự án (bao gồm 13 tools Phase 1 và 1 tool Phase 2):

| STT | Tên MCP Tool | User Story (US) | Miền Nghiệp Vụ (Domain) | Tóm Tắt Chức Năng |
| :--- | :--- | :--- | :--- | :--- |
| 1 | `search_projects` | **US1** | **Location & Project Discovery** | Tìm kiếm dự án theo từ khóa tên hoặc tỉnh/thành phố |
| 2 | `resolve_project` | **US1, US2.1, US2.2, US3** | **Location & Project Discovery** | Slot-filling: Kiểm tra văn bản người dùng có phải tên dự án hợp lệ hay không |
| 3 | `list_project_buildings` | **US1** | **Location & Project Discovery** | Liệt kê các tòa nhà/phân khu thuộc 1 dự án cụ thể |
| 4 | `list_provinces` | **US1** | **Location & Project Discovery** | Trả về danh sách các tỉnh/thành phố có dự án |
| 5 | `search_listings` | **US1** | **Listing Catalog & Product Details** | Tìm kiếm danh sách căn hộ theo bộ lọc (giá, loại hình, số phòng ngủ) |
| 6 | `get_listing` | **US1** | **Listing Catalog & Product Details** | Lấy thông tin chi tiết đầy đủ của 1 căn hộ/bất động sản |
| 7 | `list_project_listings` | **US1** | **Listing Catalog & Product Details** | Liệt kê tất cả các căn hộ trong 1 dự án ("Xem tất cả") |
| 8 | `compare_listings` | **US6** | **Listing Comparison & Micro-Analytics** | So sánh đối chiếu từ 2 đến 4 căn hộ song song (Side-by-side) |
| 9 | `project_overview` | **US4** | **Market Analytics & Overview** | **Thống kê & Phân tích tổng quan thị trường cho 1 dự án** |
| 10 | `map_listings` | **US5** | **Spatial Visualization & Map** | Trực quan hóa danh sách căn hộ trên bản đồ (tọa độ lat/lng) |
| 11 | `start_visit_booking` | **US2.1** | **Lead Generation & Action Triggering** | Khởi tạo Form đặt lịch tham quan dự án (khách vs đã xác thực) |
| 12 | `start_consultation` | **US2.2** | **Lead Generation & Action Triggering** | Khởi tạo Form đăng ký tư vấn mua nhà |
| 13 | `listing_cta_actions` | **US1** | **Lead Generation & Action Triggering** | Trả về danh sách các nút bấm hành động UI CTA dưới mỗi sản phẩm |
| 14 | `answer_project_policy` *(Phase 2)* | **US3** | **Policy, Legal & Knowledge RAG** | Tra cứu chính sách bán hàng, FAQ, pháp lý dự án qua RAG (Vector + BM25) |

---

## 📊 2. Các Tool Liên Quan Trực Tiếp & Gián Tiếp Đến "PHÂN TÍCH TỔNG QUAN"

Nếu nhiệm vụ của bạn là **phát triển tool phân tích tổng quan**, dưới đây là danh sách các tool thuộc phạm vi công việc của bạn, được phân thành **Cốt Lõi (Trực tiếp)** và **Hỗ Trợ (Gián tiếp)**:

### 2.1. Tool Cốt Lõi (Core Overview Tool)

#### 🔵 `project_overview(project_id: str)` — User Story 4 (US4)
* **Vị trí code**: `src/app/tools/analytics.py` (hàm `project_overview`) và `src/app/services/listings.py` (hàm `project_price_stats`).
* **Vai trò**: Đây chính là **Tool trung tâm và quan trọng nhất** đảm nhận nhiệm vụ phân tích tổng quan thị trường của một dự án.
* **Dữ liệu đầu ra**:
  - Tổng số lượng căn hộ đang mở bán trong dự án (`count`).
  - Thống kê khoảng giá tổng (`price_vnd`: min, max, avg).
  - Thống kê đơn giá theo m² (`price_per_m2_vnd`: min, max, avg).
  - Khoảng số lượng phòng ngủ khả dụng (`bedrooms_range`).
  - Cơ cấu phân bố theo loại hình bất động sản (`by_property_type`: căn hộ, shophouse, liền kề, biệt thự,...).
* **Định hướng phát triển & Tối ưu (Nhiệm vụ cần làm)**:
  1. **Chuyển sang Postgres RPC**: Hiện tại hàm `project_price_stats` kéo toàn bộ dòng dữ liệu của dự án về Python để tính toán `min/max/avg`. Cần viết một Stored Procedure (RPC) trên Postgres để tính aggregation trực tiếp trên DB, giúp tăng tốc độ phản hồi gấp nhiều lần.
  2. **Bổ sung các chỉ số phân tích tổng quan nâng cao**:
     - Thống kê diện tích trung bình, diện tích nhỏ nhất/lớn nhất (`area_m2`).
     - Phân bố hướng ban công phổ biến (`direction_balcony`).
     - Phân bố trạng thái pháp lý (`legal_status`).
  3. **Tuân thủ quy tắc PRD**: Trả về dữ liệu mô tả khách quan (descriptive stats), **KHÔNG** đưa ra khuyến nghị đầu tư hoặc định giá tài chính (valuation/investment advice) trong tool này.

---

### 2.2. Các Tool Hỗ Trợ & Liên Quan (Related Overview Tools)

#### 🟢 `map_listings(project_id: str | None, limit: int)` — User Story 5 (US5)
* **Vị trí code**: `src/app/tools/analytics.py` (hàm `map_listings`).
* **Vai trò**: Phân tích tổng quan dưới góc độ **Phân bố Không gian & Địa lý** (Spatial Analytics).
* **Kết nối với Phân tích tổng quan**: Trực quan hóa mặt bằng dự án trên bản đồ nhiệt/điểm tọa độ (`lat`, `lng`), giúp người dùng có cái nhìn tổng quan về vị trí địa lý của dự án và các căn hộ xung quanh.

#### 🟢 `compare_listings(listing_ids: list[str])` — User Story 6 (US6)
* **Vị trí code**: `src/app/tools/listings.py` (hàm `compare_listings`).
* **Vai trò**: Phân tích tổng quan dưới góc độ **Phân tích So sánh Vi mô (Micro-Comparison)**.
* **Kết nối với Phân tích tổng quan**: Khi người dùng muốn xem bức tranh tổng quan so sánh giữa 2–4 căn hộ cụ thể trong cùng dự án hoặc khác dự án (so sánh giá, giá/m², diện tích, hướng, pháp lý).

#### 🟢 `search_listings_by_province` & `list_provinces` *(Nâng cấp Phase 2)*
* **Vị trí code**: `src/app/tools/locations.py` & Phase 2 Roadmap.
* **Vai trò**: Phân tích tổng quan ở cấp độ **Vùng / Tỉnh Thành phố** (Macro Overview).
* **Kết nối với Phân tích tổng quan**: Giúp người dùng xem bức tranh tổng quan thị trường bất động sản theo từng Tỉnh/Thành phố (ví dụ: Hà Nội có bao nhiêu dự án, tổng số căn hộ mở bán vùng này là bao nhiêu).

---

## 🛠️ 3. Định Hướng Công Việc Cho Lập Trình Viên Phát Triển Tool Phân Tích Tổng Quan

Nhiệm vụ của bạn đối với mảng **Phân tích tổng quan (Overview Analytics)** bao gồm các bước cụ thể sau:

1. **Gia cố & Đảm bảo tính chính xác của `project_overview` hiện tại**:
   - Kiểm tra hàm ép kiểu `shaping.to_float` / `shaping.to_int` đối với cột `area_m2` và `bedrooms` để thông số thống kê không bị lệch.
   - Xử lý các căn hộ bị thiếu giá (`price_vnd` is null) để không làm sai lệch tính toán `min`, `max`, `avg`.

2. **Chuyển đổi logic tính toán thành Postgres Stored Procedure (RPC)**:
   - Thay vì chạy `listing_svc.project_price_stats(project_id)` trong Python, hãy viết RPC Postgres:
     ```sql
     CREATE OR REPLACE FUNCTION get_project_overview_stats(p_project_id TEXT)
     RETURNS JSONB AS $$
     ...
     $$ LANGUAGE plpgsql;
     ```
   - Tool `project_overview` chỉ cần gọi `supabase.rpc('get_project_overview_stats', {'p_project_id': project_id})`.

3. **Mở rộng các chiều phân tích tổng quan mới (Dự kiến Phase 2)**:
   - Thêm phân tích khoảng diện tích (ví dụ: `<50m²`, `50-80m²`, `>80m²`).
   - Thêm thống kê khoảng tầng (`floor_band`: tầng thấp, tầng trung, tầng cao).
   - Thêm tool phân tích tổng quan cấp Tỉnh/Thành phố (`province_overview`).
