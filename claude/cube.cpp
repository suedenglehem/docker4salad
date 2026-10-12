// cube.cpp — ASCII rotating cube centered at the origin.
// Usage: ./cube [a_deg] [b_deg] [c_deg]   (defaults: 30 45 0)
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

struct V { double x, y, z; };

static const int W = 72, H = 40;
static const char FACES[6] = { 'x', 'y', 'z', 'X', 'Y', 'Z' }; // -x,-y,-z,+x,+y,+z

int main(int argc, char** argv) {
    double a = argc > 1 ? atof(argv[1]) : 30.0;
    double b = argc > 2 ? atof(argv[2]) : 45.0;
    double c = argc > 3 ? atof(argv[3]) : 0.0;

    auto rad = [](double d) { return d * M_PI / 180.0; };
    double ca = cos(rad(a)), sa = sin(rad(a));
    double cb = cos(rad(b)), sb = sin(rad(b));
    double cc = cos(rad(c)), sc = sin(rad(c));

    // Rotation R = Rz(c) * Ry(b) * Rx(a)
    auto rot = [&](V p) -> V {
        // Rx
        double y1 = p.y * ca - p.z * sa, z1 = p.y * sa + p.z * ca;
        // Ry
        double x2 = p.x * cb + z1 * sb,   z2 = -p.x * sb + z1 * cb;
        // Rz
        double x3 = x2 * cc - y1 * sc,    y3 = x2 * sc + y1 * cc;
        return {x3, y3, z2};
    };

    // 8 corners of unit cube centered at origin
    std::vector<V> C(8);
    for (int i = 0; i < 8; i++)
        C[i] = { i & 1 ? 1.0 : -1.0, i & 2 ? 1.0 : -1.0, i & 4 ? 1.0 : -1.0 };
    for (auto& p : C) p = rot(p);

    // 6 faces: index triplets into C. Winding chosen so outward normal
    // (cross of two edges) points away from the origin in unrotated space.
    int F[6][4] = {
        {0, 1, 3, 2}, // -x
        {4, 6, 7, 5}, // -y
        {0, 4, 5, 1}, // -z
        {3, 7, 6, 2}, // +x
        {2, 3, 7, 6}, // +y
        {1, 5, 7, 3}, // +z
    };

    // Screen: x right, y up (flipped on print). Scale to fit.
    const double S = 12.0;
    std::vector<std::string> grid(H, std::string(W, ' '));

    // Painter's algorithm: draw far faces first (normal.z < 0 faces away...
    // we want back-to-front: sort by mean z ascending).
    int order[6];
    for (int i = 0; i < 6; i++) order[i] = i;
    for (int i = 0; i < 6; i++)
        for (int j = i + 1; j < 6; j++) {
            auto mz = [&](int f) {
                double m = 0;
                for (int k = 0; k < 4; k++) m += C[F[f][k]].z;
                return m / 4;
            };
            if (mz(order[j]) < mz(order[i])) std::swap(order[i], order[j]);
        }

    for (int oi = 0; oi < 6; oi++) {
        int f = order[oi];
        V p0 = C[F[f][0]], p1 = C[F[f][1]], p2 = C[F[f][2]];
        // outward normal via cross product of two face edges
        V n{ (p1.y - p0.y) * (p2.z - p0.z) - (p1.z - p0.z) * (p2.y - p0.y),
             (p1.z - p0.z) * (p2.x - p0.x) - (p1.x - p0.x) * (p2.z - p0.z),
             (p1.x - p0.x) * (p2.y - p0.y) - (p1.y - p0.y) * (p2.x - p0.x) };
        double nl = sqrt(n.x*n.x + n.y*n.y + n.z*n.z);
        if (nl < 1e-9) continue;
        n.x /= nl; n.y /= nl; n.z /= nl;
        // backface cull: only draw faces pointing toward viewer (+z)
        if (n.z <= 0.02) continue;

        char ch = FACES[f];
        // rasterize the quad by scanning its bounding box on screen
        double minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9;
        for (int k = 0; k < 4; k++) {
            V p = C[F[f][k]];
            minx = std::min(minx, p.x * S / 2 + W / 2);
            maxx = std::max(maxx, p.x * S / 2 + W / 2);
            miny = std::min(miny, p.y * S + H / 2);
            maxy = std::max(maxy, p.y * S + H / 2);
        }
        auto inside = [&](double sx, double sy) -> bool {
            // point-in-quad via winding number in screen space
            int wn = 0;
            for (int k = 0; k < 4; k++) {
                V pa = C[F[f][k]], pb = C[F[f][(k + 1) % 4]];
                double ax = pa.x * S / 2 + W / 2, ay = pa.y * S + H / 2;
                double bx = pb.x * S / 2 + W / 2, by = pb.y * S + H / 2;
                if (ay <= sy) {
                    if (by > sy && (bx - ax) * (sy - ay) > (by - ay) * (sx - ax)) wn++;
                } else {
                    if (by <= sy && (bx - ax) * (sy - ay) < (by - ay) * (sx - ax)) wn--;
                }
            }
            return wn != 0;
        };
        for (int iy = (int)miny; iy <= (int)maxy; iy++)
            for (int ix = (int)minx; ix <= (int)maxx; ix++)
                if (inside(ix + 0.5, iy + 0.5)) grid[iy][ix] = ch;
    }

    // print top row first
    for (int y = H - 1; y >= 0; y--) printf("%s\n", grid[y].c_str());
    return 0;
}
