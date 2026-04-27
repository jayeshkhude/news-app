import re

def main():
    with open('frontend/index.html', 'r', encoding='utf-8') as f:
        idx = f.read()
        
    nav_css_match = re.search(r'(\.nav \{[\s\S]*?)(?=\.ag-hero \{)', idx)
    nav_css = nav_css_match.group(1) if nav_css_match else ""
    
    nav_html_match = re.search(r'(<nav class="nav" id="mainNav"[\s\S]*?</div>\n</div>)', idx)
    nav_html = nav_html_match.group(1) if nav_html_match else ""
    
    if not nav_css or not nav_html:
        print("Failed to extract from index")
        return
        
    body_css_match = re.search(r'(body \{[\s\S]*?\})', idx)
    body_css = body_css_match.group(1) if body_css_match else ""

    for page in ['frontend/reels.html', 'frontend/contact.html', 'frontend/analytics.html']:
        with open(page, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # replace topbar CSS
        content = re.sub(r'\.topbar \{[\s\S]*?(?=\.progress-wrap \{|\.wrap \{|\.header \{)', nav_css, content)
        
        # replace body CSS
        if body_css:
            content = re.sub(r'body \{[^}]+\}', body_css, content)
        
        # replace topbar HTML
        content = re.sub(r'<div class="topbar">[\s\S]*?</div>\s*</div>', nav_html, content)
        
        with open(page, 'w', encoding='utf-8') as f:
            f.write(content)
            
    print("Successfully synced nav to other pages")

if __name__ == '__main__':
    main()
