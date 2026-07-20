Caso queiram personalizar o bookstack
```css

<style>

body, .page-content, .book-content, .chapter-content, .shelves-list {
  font-family: "Georgia", "Times New Roman", Times, serif !important;
  color: #3a3a3a;
  background-color: #EFEFEF; 
  line-height: 1.6;
}

#content, .content-wrap {
  background-color: #EFEFEF !important;
}

#header, .header, header.header, .top-header {
  background-color: #D9D9D9 !important; 
  border-bottom: 1px solid #9A9EA2;
}

#header a, .header a,
#header .logo, .header .logo,
#header .logo a, .header .logo a,
#header .header-links a, .header .header-links a,
#header .dropdown-container a, .header .dropdown-container a,
#header .dropdown-toggle, .header .dropdown-toggle {
  color: #2f3234 !important;
}

#header a:hover, .header a:hover {
  color: #17181a !important;
}

#header .logo svg, .header .logo svg,
#header svg, .header svg {
  fill: #2f3234 !important;
}

  #header .logo, .header .logo,
#header .logo span, .header .logo span,
#header .logo-image + *, 
.header .logo-text {
  color: #2f3234 !important;
}

#header .dropdown-container .text-link,
.header .dropdown-container .text-link,
#header .dropdown-container button,
.header .dropdown-container button {
  color: #2f3234 !important;
}

#header input[type="search"],
#header input[type="text"],
.search-box input,
.header-search input {
  background-color: #C7CACD !important;
  border: 1px solid #9A9EA2 !important;
  color: #2f3234 !important;
  border-radius: 3px;
}

#header input[type="search"]::placeholder,
.search-box input::placeholder {
  color: #55585b !important;
}

#header .search-box svg, .header-search svg,
#header .search-box .svg-icon, .header-search .svg-icon {
  fill: #55585b !important;
}

h1, h2, h3, h4, h5, h6,
.page-content h1, .page-content h2, .page-content h3,
.book-content h1, .book-content h2,
.chapter-content h1, .chapter-content h2 {
  font-family: "Georgia", "Times New Roman", serif !important;
  font-weight: 700;
  letter-spacing: 0.5px;
  color: #33404d;
  border-bottom: 3px double #A9C1D9;
  padding-bottom: 6px;
  margin-top: 1.4em;
  text-transform: none;
}

.page-display > h1,
.book-content h1.book-title,
h1.header-title,
.content-header h1 {
  text-transform: uppercase;
  font-size: 2.2rem;
  letter-spacing: 1px;
  color: #33404d;
  border-top: 4px solid #A9C1D9;
  border-bottom: 4px solid #A9C1D9;
  padding: 10px 0;
  text-align: center;
}

.page-content {
  max-width: 900px;
  margin: 0 auto;
}

.page-content blockquote {
  border-left: 4px solid #A9C1D9;
  font-style: italic;
  padding: 8px 16px;
  background-color: #eee4da;
  margin: 16px 0;
}

.shelves-list .grid-card,
.book-grid-item,
.entity-list .list-item-book,
.entity-list .list-item-chapter,
.entity-list .list-item-page {
  background-color: #fdfaf5 !important;
  border: 1px solid #e0d6c8 !important;
  border-radius: 4px !important;
  box-shadow: none !important;
}

.shelves-list .grid-card:hover,
.book-grid-item:hover {
  box-shadow: 2px 2px 0 #A9C1D9 !important;
  transition: box-shadow 0.15s ease-in-out;
}

.page-content a,
.book-content a,
.chapter-content a,
.shelves-list a {
  color: #B96A5E !important; 
  text-decoration: none;
  border-bottom: 1px solid #B96A5E;
}

.page-content a:hover,
.book-content a:hover,
.chapter-content a:hover,
.shelves-list a:hover {
  color: #954E44 !important;
  border-bottom: 1px solid #954E44;
  background-color: #f2e2dd;
}

.page-content a:visited {
  color: #A5827C !important;
}

.sidebar-page-list, #sidebar {
  background-color: #D9D9D9 !important;
  border-right: 1px solid #9A9EA2;
}

.tri-layout-header-actions, .action-buttons, .header-secondary {
  background-color: #F5F5F5 !important;
}

.sidebar-page-list a, #sidebar a {
  color: #3a3a3a !important;
}

.sidebar-page-list a:hover, #sidebar a:hover {
  color: #B96A5E !important;
}

.page-content table {
  border-collapse: collapse;
  width: 100%;
}

.page-content table th {
  border-top: 2px solid #A9C1D9;
  border-bottom: 1px solid #A9C1D9;
  text-transform: uppercase;
  font-size: 0.85rem;
  letter-spacing: 0.5px;
}

.page-content table td {
  border-bottom: 1px solid #e0d6c8;
}

.page-metadata, .content-meta {
  font-style: italic;
  color: #6b6b6b;
  font-size: 0.85rem;
  border-top: 1px solid #e0d6c8;
  padding-top: 6px;
}
</style>
```
